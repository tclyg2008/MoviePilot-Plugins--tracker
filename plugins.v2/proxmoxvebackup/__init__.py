import hashlib
import json
import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from pathlib import Path

import pytz
import paramiko
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class ProxmoxVEBackup(_PluginBase):
    # 插件名称
    plugin_name = "PVE虚拟机守护神"
    # 插件描述
    plugin_desc = "PVE虚拟机守护神，自动化备份与恢复容器，提供完整的备份管理解决方案。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins/refs/heads/main/icons/proxmox.webp"
    # 插件版本
    plugin_version = "1.1.5"
    # 插件作者
    plugin_author = "M.Jinxi"
    # 作者主页
    author_url = "https://github.com/xijin285"
    # 插件配置项ID前缀
    plugin_config_prefix = "proxmox_backup_"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: Optional[threading.Lock] = None
    _running: bool = False
    _backup_activity: str = "空闲"
    _restore_activity: str = "空闲"
    _max_history_entries: int = 100 # Max number of history entries to keep
    _restore_lock: Optional[threading.Lock] = None  # 恢复操作锁
    _max_restore_history_entries: int = 50  # 恢复历史记录最大数量
    _global_task_lock: Optional[threading.Lock] = None  # 全局任务锁，协调备份和恢复任务
    _last_config_hash: Optional[str] = None  # 上次配置的哈希值

    # 配置属性
    _enabled: bool = False
    _cron: str = "0 3 * * *"
    _onlyonce: bool = False
    _notify: bool = False
    _retry_count: int = 0  # 默认不重试
    _retry_interval: int = 60
    _notification_message_type: str = "Plugin"  # 新增：消息类型
    
    # SSH配置
    _pve_host: str = ""  # PVE主机地址
    _ssh_port: int = 22
    _ssh_username: str = "root"
    _ssh_password: str = ""
    _ssh_key_file: str = ""

    # 备份配置
    _enable_local_backup: bool = True  # 本地备份开关
    _backup_path: str = ""
    _keep_backup_num: int = 7
    _backup_vmid: str = ""  # 要备份的容器ID，逗号分隔
    _storage_name: str = "local"  # 存储名称
    _backup_mode: str = "snapshot"  # 备份模式，默认snapshot
    _compress_mode: str = "zstd"    # 压缩模式，默认zstd
    _auto_delete_after_download: bool = False  # 下载后自动删除PVE备份
    _download_all_backups: bool = False  # 下载所有备份文件（多VM备份时）

    # WebDAV配置
    _enable_webdav: bool = False
    _webdav_url: str = ""
    _webdav_username: str = ""
    _webdav_password: str = ""
    _webdav_path: str = ""
    _webdav_keep_backup_num: int = 7
    _clear_history: bool = False  # 清理历史记录开关

    # 恢复配置
    _enable_restore: bool = False  # 启用恢复功能
    _restore_storage: str = "local"  # 恢复存储名称
    _restore_vmid: str = ""  # 恢复目标VMID
    _restore_force: bool = False  # 强制恢复（覆盖现有VM）
    _restore_skip_existing: bool = True  # 跳过已存在的VM
    _restore_file: str = "" # 要恢复的文件
    _restore_now: bool = False # 立即恢复开关
    _stopped: bool = False  # 增加已停止标志
    _instance = None  # 单例实例

    def init_plugin(self, config: Optional[dict] = None):
        # 加载上次的配置哈希
        self._last_config_hash = self.get_data('last_config_hash')
        
        # 检查是否真的需要重新初始化
        if self._should_skip_reinit(config):
            logger.debug(f"{self.plugin_name} 配置未发生实质性变更，跳过重新初始化")
            return
            
        # 确保先停止已有的服务
        self._stopped = False  # 启动前重置停止标志
        self.stop_service()
        
        self._lock = threading.Lock()
        self._restore_lock = threading.Lock()  # 初始化恢复锁
        self._global_task_lock = threading.Lock()  # 初始化全局任务锁

        # 首先加载已保存的配置
        saved_config = self.get_config()
        if saved_config:
            # 使用已保存的配置更新默认值
            self._enabled = bool(saved_config.get("enabled", False))
            self._cron = str(saved_config.get("cron", "0 3 * * *"))
            self._onlyonce = bool(saved_config.get("onlyonce", False))
            self._notify = bool(saved_config.get("notify", False))
            self._retry_count = int(saved_config.get("retry_count", 0))
            self._retry_interval = int(saved_config.get("retry_interval", 60))
            self._notification_message_type = str(saved_config.get("notification_message_type", "Plugin"))  # 新增
            
            # SSH配置
            self._pve_host = str(saved_config.get("pve_host", ""))
            self._ssh_port = int(saved_config.get("ssh_port", 22))
            self._ssh_username = str(saved_config.get("ssh_username", "root"))
            self._ssh_password = str(saved_config.get("ssh_password", ""))
            self._ssh_key_file = str(saved_config.get("ssh_key_file", ""))
            
            # 备份配置
            self._storage_name = str(saved_config.get("storage_name", "local"))
            self._enable_local_backup = bool(saved_config.get("enable_local_backup", True))
            self._backup_mode = str(saved_config.get("backup_mode", "snapshot"))
            self._compress_mode = str(saved_config.get("compress_mode", "zstd"))
            self._backup_vmid = str(saved_config.get("backup_vmid", ""))
            self._auto_delete_after_download = bool(saved_config.get("auto_delete_after_download", False))
            self._download_all_backups = bool(saved_config.get("download_all_backups", False))
            
            configured_backup_path = str(saved_config.get("backup_path", "")).strip()
            if not configured_backup_path:
                self._backup_path = str(self.get_data_path() / "actual_backups")
                logger.info(f"{self.plugin_name} 备份文件存储路径未配置，使用默认: {self._backup_path}")
            else:
                self._backup_path = configured_backup_path
            self._keep_backup_num = int(saved_config.get("keep_backup_num", 7))
            
            # WebDAV配置
            self._enable_webdav = bool(saved_config.get("enable_webdav", False))
            self._webdav_url = str(saved_config.get("webdav_url", ""))
            self._webdav_username = str(saved_config.get("webdav_username", ""))
            self._webdav_password = str(saved_config.get("webdav_password", ""))
            self._webdav_path = str(saved_config.get("webdav_path", ""))
            self._webdav_keep_backup_num = int(saved_config.get("webdav_keep_backup_num", 7))
            self._clear_history = bool(saved_config.get("clear_history", False))

            # 恢复配置
            self._enable_restore = bool(saved_config.get("enable_restore", False))
            self._restore_storage = str(saved_config.get("restore_storage", "local"))
            self._restore_vmid = str(saved_config.get("restore_vmid", ""))
            self._restore_force = bool(saved_config.get("restore_force", False))
            self._restore_skip_existing = bool(saved_config.get("restore_skip_existing", True))
            self._restore_file = str(saved_config.get("restore_file", ""))
            self._restore_now = bool(saved_config.get("restore_now", False))

        # 如果有新的配置传入，使用新配置覆盖
        if config:
            if "enabled" in config:
                self._enabled = bool(config["enabled"])
            if "cron" in config:
                self._cron = str(config["cron"])
            if "onlyonce" in config:
                self._onlyonce = bool(config["onlyonce"])
            if "notify" in config:
                self._notify = bool(config["notify"])
            if "retry_count" in config:
                self._retry_count = int(config["retry_count"])
            if "retry_interval" in config:
                self._retry_interval = int(config["retry_interval"])
            if "notification_message_type" in config:
                self._notification_message_type = str(config["notification_message_type"])
            
            # SSH配置
            if "pve_host" in config:
                self._pve_host = str(config["pve_host"])
            if "ssh_port" in config:
                self._ssh_port = int(config["ssh_port"])
            if "ssh_username" in config:
                self._ssh_username = str(config["ssh_username"])
            if "ssh_password" in config:
                self._ssh_password = str(config["ssh_password"])
            if "ssh_key_file" in config:
                self._ssh_key_file = str(config["ssh_key_file"])
            
            # 备份配置
            if "storage_name" in config:
                self._storage_name = str(config["storage_name"])
            if "enable_local_backup" in config:
                self._enable_local_backup = bool(config["enable_local_backup"])
            if "backup_mode" in config:
                self._backup_mode = str(config["backup_mode"])
            if "compress_mode" in config:
                self._compress_mode = str(config["compress_mode"])
            if "backup_vmid" in config:
                self._backup_vmid = str(config["backup_vmid"])
            if "auto_delete_after_download" in config:
                self._auto_delete_after_download = bool(config["auto_delete_after_download"])
            if "download_all_backups" in config:
                self._download_all_backups = bool(config["download_all_backups"])
            
            if "backup_path" in config:
                configured_backup_path = str(config["backup_path"]).strip()
                if not configured_backup_path:
                    self._backup_path = str(self.get_data_path() / "actual_backups")
                    logger.info(f"{self.plugin_name} 备份文件存储路径未配置，使用默认: {self._backup_path}")
                else:
                    self._backup_path = configured_backup_path
            if "keep_backup_num" in config:
                self._keep_backup_num = int(config["keep_backup_num"])
            
            # WebDAV配置
            if "enable_webdav" in config:
                self._enable_webdav = bool(config["enable_webdav"])
            if "webdav_url" in config:
                self._webdav_url = str(config["webdav_url"])
            if "webdav_username" in config:
                self._webdav_username = str(config["webdav_username"])
            if "webdav_password" in config:
                self._webdav_password = str(config["webdav_password"])
            if "webdav_path" in config:
                self._webdav_path = str(config["webdav_path"])
            if "webdav_keep_backup_num" in config:
                self._webdav_keep_backup_num = int(config["webdav_keep_backup_num"])
            if "clear_history" in config:
                self._clear_history = bool(config["clear_history"])
            
            # 恢复配置
            if "enable_restore" in config:
                self._enable_restore = bool(config["enable_restore"])
            if "restore_storage" in config:
                self._restore_storage = str(config["restore_storage"])
            if "restore_vmid" in config:
                self._restore_vmid = str(config["restore_vmid"])
            if "restore_force" in config:
                self._restore_force = bool(config["restore_force"])
            if "restore_skip_existing" in config:
                self._restore_skip_existing = bool(config["restore_skip_existing"])
            if "restore_file" in config:
                self._restore_file = str(config["restore_file"])
            if "restore_now" in config:
                self._restore_now = bool(config["restore_now"])
            
            self.__update_config()

            # 处理清理历史记录请求
            if self._clear_history:
                self._clear_all_history()
                self._clear_history = False
                self.__update_config()

            # 处理立即恢复请求
            if self._restore_now and self._restore_file:
                try:
                    source, filename = self._restore_file.split('|', 1)
                    # 在新线程中运行恢复任务，避免阻塞
                    threading.Thread(target=self.run_restore_job, args=(filename, source)).start()
                    logger.info(f"{self.plugin_name} 已触发恢复任务，文件: {filename}")
                except Exception as e:
                    logger.error(f"{self.plugin_name} 触发恢复任务失败: {e}")
                finally:
                    # 重置开关状态
                    self._restore_now = False
                    self._restore_file = ""
                    self.__update_config()

        try:
            Path(self._backup_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
             logger.error(f"{self.plugin_name} 创建实际备份目录 {self._backup_path} 失败: {e}")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                try:
                    # 创建新的调度器
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    job_name = f"{self.plugin_name}服务_onlyonce"
                    
                    # 移除同名任务(如果存在)
                    if self._scheduler.get_job(job_name):
                        self._scheduler.remove_job(job_name)
                        
                    logger.info(f"{self.plugin_name} 服务启动，立即运行一次")
                    self._scheduler.add_job(func=self.run_backup_job, trigger='date',
                                         run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                         name=job_name, id=job_name)
                    self._onlyonce = False
                    self.__update_config()
                    
                    # 启动调度器
                    if not self._scheduler.running:
                        self._scheduler.start()
                except Exception as e:
                    logger.error(f"启动一次性 {self.plugin_name} 任务失败: {str(e)}")

        ProxmoxVEBackup._instance = self  # 注册单例

    def _should_skip_reinit(self, config: Optional[dict] = None) -> bool:
        """
        检查是否应该跳过重新初始化
        只有在关键配置发生变更时才重新初始化
        """
        if not config:
            return False
            
        # 检查特殊操作标志（这些操作需要立即执行）
        special_operations = {'clear_history', 'restore_now'}
        for op in special_operations:
            if op in config and config[op]:
                logger.debug(f"{self.plugin_name} 检测到特殊操作: {op}，需要重新初始化")
                return False
            
        # 计算当前配置的哈希值
        current_config_hash = self._calculate_config_hash(config)
        
        # 如果哈希值相同，说明配置没有实质性变更
        if self._last_config_hash == current_config_hash:
            logger.debug(f"{self.plugin_name} 配置哈希未变更，跳过重新初始化 (哈希: {current_config_hash[:8]}...)")
            return True
            
        # 更新哈希值
        self._last_config_hash = current_config_hash
        logger.debug(f"{self.plugin_name} 配置哈希已变更，需要重新初始化 (旧哈希: {self._last_config_hash[:8] if self._last_config_hash else 'None'}... -> 新哈希: {current_config_hash[:8]}...)")
        return False

    def _calculate_config_hash(self, config: dict) -> str:
        """
        计算配置的哈希值，用于检测配置变更
        """
        try:
            # 只考虑影响服务行为的关键配置项
            critical_config = {}
            critical_keys = {
                'enabled', 'cron', 'onlyonce', 'notify', 'retry_count', 'retry_interval',
                'pve_host', 'ssh_port', 'ssh_username', 'ssh_password', 'ssh_key_file',
                'storage_name', 'backup_vmid', 'enable_local_backup', 'backup_path',
                'keep_backup_num', 'backup_mode', 'compress_mode',
                'enable_webdav', 'webdav_url', 'webdav_username', 'webdav_password',
                'webdav_path', 'webdav_keep_backup_num',
                'enable_restore', 'restore_storage', 'restore_vmid', 'restore_force',
                'restore_skip_existing', 'restore_file', 'restore_now'
            }
            
            for key in critical_keys:
                if key in config:
                    critical_config[key] = config[key]
            
            # 将配置转换为JSON字符串并计算哈希
            import json
            config_str = json.dumps(critical_config, sort_keys=True, ensure_ascii=False)
            return hashlib.md5(config_str.encode('utf-8')).hexdigest()
            
        except Exception as e:
            logger.error(f"{self.plugin_name} 计算配置哈希失败: {e}")
            # 如果计算失败，返回一个固定值，确保不会跳过初始化
            return "error_hash"

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "retry_count": self._retry_count,
            "retry_interval": self._retry_interval,
            "notification_message_type": self._notification_message_type,  # 新增
            
            # SSH配置
            "pve_host": self._pve_host,
            "ssh_port": self._ssh_port,
            "ssh_username": self._ssh_username,
            "ssh_password": self._ssh_password,
            "ssh_key_file": self._ssh_key_file,
            
            # 备份配置
            "storage_name": self._storage_name,
            "backup_vmid": self._backup_vmid,
            "enable_local_backup": self._enable_local_backup,
            "backup_path": self._backup_path,
            "keep_backup_num": self._keep_backup_num,
            "backup_mode": self._backup_mode,
            "compress_mode": self._compress_mode,
            "auto_delete_after_download": self._auto_delete_after_download,
            "download_all_backups": self._download_all_backups,
            
            # WebDAV配置
            "enable_webdav": self._enable_webdav,
            "webdav_url": self._webdav_url,
            "webdav_username": self._webdav_username,
            "webdav_password": self._webdav_password,
            "webdav_path": self._webdav_path,
            "webdav_keep_backup_num": self._webdav_keep_backup_num,
            "clear_history": self._clear_history,
            
            # 恢复配置
            "enable_restore": self._enable_restore,
            "restore_storage": self._restore_storage,
            "restore_vmid": self._restore_vmid,
            "restore_force": self._restore_force,
            "restore_skip_existing": self._restore_skip_existing,
            "restore_file": self._restore_file,
            "restore_now": self._restore_now,
        })
        
        # 保存配置哈希
        if self._last_config_hash:
            self.save_data('last_config_hash', self._last_config_hash)

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """添加恢复API接口"""
        return [
            {
                "path": "/restore",
                "endpoint": api_restore_backup,  # 直接引用本地函数对象
                "methods": ["POST"],
                "description": "执行恢复操作"
            }
        ]

    @classmethod
    def get_instance(cls):
        return cls._instance

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "ProxmoxVEBackupService",
                        "name": f"{self.plugin_name}定时服务",
                        "trigger": CronTrigger.from_crontab(self._cron, timezone=settings.TZ),
                        "func": self.run_backup_job,
                        "kwargs": {}
                    }]
                else:
                    logger.error(f"{self.plugin_name} cron表达式格式错误: {self._cron}")
                    return []
            except Exception as err:
                logger.error(f"{self.plugin_name} 定时任务配置错误：{str(err)}")
                return []
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 获取当前保存的配置
        current_config = self.get_config()
        if current_config is None:
            current_config = {}

        # 动态生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })

        # 定义基础设置内容
        basic_settings = [
            {
                'component': 'VCardTitle',
                'props': {'class': 'text-h6'},
                'text': '⚙️ 基础设置'
            },
            {
                'component': 'VCardText',
                'content': [
                    # 开关行
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件', 'color': 'primary', 'prepend-icon': 'mdi-power'}}]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知', 'color': 'info', 'prepend-icon': 'mdi-bell'}}]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次', 'color': 'success', 'prepend-icon': 'mdi-play'}}]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清理历史记录', 'color': 'warning', 'prepend-icon': 'mdi-delete-sweep'}}]},
                        ],
                    },
                    # 4个一排：失败重试次数、重试间隔、执行周期、消息类型
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [
                                {'component': 'VTextField', 'props': {
                                    'model': 'retry_count',
                                    'label': '失败重试次数',
                                    'type': 'number',
                                    'placeholder': '默认为0(不重试)',
                                    'hint': '建议设置为0',
                                    'persistent-hint': True,
                                    'prepend-inner-icon': 'mdi-refresh'
                                }}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [
                                {'component': 'VTextField', 'props': {
                                    'model': 'retry_interval',
                                    'label': '重试间隔(秒)',
                                    'type': 'number',
                                    'placeholder': '默认为60秒',
                                    'prepend-inner-icon': 'mdi-timer'
                                }}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [
                                {'component': 'VCronField', 'props': {
                                    'model': 'cron',
                                    'label': '执行周期',
                                    'prepend-inner-icon': 'mdi-clock-outline'
                                }}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 3}, 'content': [
                                {'component': 'VSelect', 'props': {
                                    'model': 'notification_message_type',
                                    'label': '消息类型',
                                    'items': MsgTypeOptions,
                                    'prepend-inner-icon': 'mdi-message-alert'
                                }}
                            ]},
                        ]
                    },
                ]
            }
        ]
        
        # 定义选项卡内容
        tabs = {
            'connection': {
                'icon': 'mdi-connection', 'title': '连接设置', 'content': [
                    # PVE连接设置
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '🔌 PVE主机'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'pve_host',
                                                    'label': 'PVE主机地址',
                                                    'placeholder': '例如: 192.168.1.100',
                                                    'prepend-inner-icon': 'mdi-server'
                                                }}
                                            ]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'ssh_port',
                                                    'label': 'SSH端口',
                                                    'type': 'number',
                                                    'placeholder': '默认为22',
                                                    'prepend-inner-icon': 'mdi-numeric'
                                                }}
                                            ]},
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'ssh_username',
                                                    'label': 'SSH用户名',
                                                    'placeholder': '默认为root',
                                                    'persistent-hint': True,
                                                    'hint': '通常使用root用户以确保有足够权限',
                                                    'prepend-inner-icon': 'mdi-account'
                                                }}
                                            ]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'ssh_password',
                                                    'label': 'SSH密码',
                                                    'type': 'password',
                                                    'placeholder': '如使用密钥认证可留空',
                                                    'prepend-inner-icon': 'mdi-key'
                                                }}
                                            ]},
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'ssh_key_file',
                                                    'label': 'SSH私钥文件路径',
                                                    'placeholder': '如使用密码认证可留空',
                                                    'prepend-inner-icon': 'mdi-file-key'
                                                }}
                                            ]},
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            'storage': {
                'icon': 'mdi-database-outline', 'title': '存储设置', 'content': [
                    # 本地备份设置卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '💾 本地备份设置'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enable_local_backup', 'label': '启用本地备份', 'color': 'primary', 'prepend-icon': 'mdi-folder'}}]},
                                        ],
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 8}, 'content': [{'component': 'VTextField', 'props': {'model': 'backup_path', 'label': '备份文件存储路径', 'placeholder': '留空则使用默认路径', 'prepend-inner-icon': 'mdi-folder-open'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'keep_backup_num', 'label': '本地备份保留数量', 'type': 'number', 'placeholder': '例如: 7', 'prepend-inner-icon': 'mdi-counter'}}]},
                                        ],
                                    },
                                ]
                            }
                        ]
                    },
                    # WebDAV远程备份设置卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '☁️ WebDAV远程备份设置'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enable_webdav', 'label': '启用WebDAV备份', 'color': 'primary', 'prepend-icon': 'mdi-cloud-upload'}}]},
                                        ],
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'webdav_url', 'label': 'WebDAV服务器地址', 'placeholder': '例如: https://dav.jianguoyun.com/dav/', 'prepend-inner-icon': 'mdi-cloud'}}]},
                                        ],
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'webdav_username', 'label': 'WebDAV用户名', 'placeholder': '请输入用户名', 'prepend-inner-icon': 'mdi-account'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'webdav_password', 'label': 'WebDAV密码', 'type': 'password', 'placeholder': '请输入密码', 'prepend-inner-icon': 'mdi-lock'}}]},
                                        ],
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 8}, 'content': [{'component': 'VTextField', 'props': {'model': 'webdav_path', 'label': 'WebDAV备份路径', 'placeholder': '例如: /backups/proxmox', 'prepend-inner-icon': 'mdi-folder-network'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'webdav_keep_backup_num', 'label': 'WebDAV备份保留数量', 'type': 'number', 'placeholder': '例如: 7', 'prepend-inner-icon': 'mdi-counter'}}]},
                                        ],
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
            'task': {
                'icon': 'mdi-clipboard-list-outline', 'title': '备份设置', 'content': [
                    # 备份任务配置卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📋 备份任务配置'},
                            {'component': 'VCardText', 'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VTextField', 'props': {
                                                'model': 'storage_name',
                                                'label': '存储名称',
                                                'placeholder': '如 local、PVE，默认为 local',
                                                'prepend-inner-icon': 'mdi-database'
                                            }}
                                        ]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VTextField', 'props': {
                                                'model': 'backup_vmid',
                                                'label': '要备份的容器ID',
                                                'placeholder': '多个ID用英文逗号分隔，如102,103，留空则备份全部',
                                                'prepend-inner-icon': 'mdi-numeric'
                                            }}
                                        ]},
                                    ]
                                },
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VSelect', 'props': {
                                                'model': 'backup_mode',
                                                'label': '备份模式',
                                                'items': [
                                                    {'title': '快照（推荐，支持快照卷）', 'value': 'snapshot'},
                                                    {'title': '挂起（suspend挂起）', 'value': 'suspend'},
                                                    {'title': '关机（stop关机）', 'value': 'stop'},
                                                ],
                                                'prepend-inner-icon': 'mdi-camera-timer'
                                            }}
                                        ]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VSelect', 'props': {
                                                'model': 'compress_mode',
                                                'label': '压缩模式',
                                                'items': [
                                                    {'title': 'ZSTD（又快又好）', 'value': 'zstd'},
                                                    {'title': 'GZIP（兼容性好）', 'value': 'gzip'},
                                                    {'title': 'LZO（速度快）', 'value': 'lzo'},
                                                ],
                                                'prepend-inner-icon': 'mdi-zip-box'
                                            }}
                                        ]},
                                    ]
                                },
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VSwitch', 'props': {'model': 'auto_delete_after_download', 'label': '下载后自动删除PVE备份', 'color': 'error', 'prepend-icon': 'mdi-delete-forever'}},
                                        ]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VSwitch', 'props': {'model': 'download_all_backups', 'label': '下载所有备份文件（多VM时）', 'color': 'info', 'prepend-icon': 'mdi-download-multiple'}},
                                        ]},
                                    ],
                                }
                            ]}
                        ]
                    }
                ]
            },
            'restore': {
                'icon': 'mdi-restore', 'title': '恢复设置', 'content': [
                    # 恢复功能设置卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '🔄 恢复功能设置'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enable_restore', 'label': '启用恢复功能', 'color': 'primary', 'prepend-icon': 'mdi-restore'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'restore_force', 'label': '强制恢复（覆盖现有VM）', 'color': 'error', 'prepend-icon': 'mdi-alert-circle'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'restore_skip_existing', 'label': '跳过已存在的VM', 'color': 'warning', 'prepend-icon': 'mdi-skip-next'}}]},
                                        ],
                                },
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'restore_storage',
                                                    'label': '恢复存储名称',
                                                    'placeholder': '如 local、PVE，默认为 local',
                                                    'prepend-inner-icon': 'mdi-database'
                                                }}
                                        ]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField', 'props': {
                                                    'model': 'restore_vmid',
                                                    'label': '恢复目标VMID',
                                                    'placeholder': '留空则使用备份文件中的原始VMID',
                                                    'prepend-inner-icon': 'mdi-numeric'
                                                }}
                                            ]},
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 8}, 'content': [
                                                {'component': 'VSelect', 'props': {
                                                    'model': 'restore_file',
                                                    'label': '选择要恢复的备份文件',
                                                    'items': [
                                                        {'title': f"{backup['filename']} ({backup['source']})", 'value': f"{backup['source']}|{backup['filename']}"}
                                                        for backup in self._get_available_backups()
                                                    ],
                                                    'placeholder': '请选择一个备份文件',
                                                    'prepend-inner-icon': 'mdi-file-find'
                                                }}
                                            ]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                                {'component': 'VSwitch', 'props': {'model': 'restore_now', 'label': '立即恢复', 'color': 'success', 'prepend-icon': 'mdi-play-circle'}}
                                            ]},
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 恢复功能说明卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '📋 恢复功能说明'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'class': 'mb-2'
                                        },
                                        'content': [
                                            {'component': 'VListItem', 'props': {'prepend-icon': 'mdi-information-outline'}, 'content': [{'component': 'VListItemTitle', 'text': '【恢复功能】'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 支持从本地备份文件恢复虚拟机'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 支持从WebDAV备份文件恢复虚拟机'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 可在插件页面选择备份文件进行恢复'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 支持强制恢复覆盖现有虚拟机'}]},
                                        ]
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'class': 'mb-2'
                                        },
                                        'content': [
                                            {'component': 'VListItem', 'props': {'prepend-icon': 'mdi-alert-circle-outline'}, 'content': [{'component': 'VListItemTitle', 'text': '【恢复注意事项】'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 恢复操作会停止目标虚拟机（如果正在运行）'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 强制恢复会删除现有的同名虚拟机'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 建议在恢复前手动备份重要数据'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '• 恢复过程可能需要较长时间，请耐心等待'}]},
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            'readme': {
                'icon': 'mdi-book-open-variant', 'title': '使用说明', 'content': [
                    # 使用说明卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'text-h6'},
                                'text': '📖 插件使用说明'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'class': 'mb-2'
                                        },
                                        'content': [
                                            {'component': 'VListItem', 'props': {'prepend-icon': 'mdi-star-circle-outline'}, 'content': [{'component': 'VListItemTitle', 'text': '【基础使用说明】'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '1. 在 [连接设置] 中，填写PVE主机地址和SSH连接信息。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '2. 在 [备份设置] 中，设置要备份的容器ID、备份模式等。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '3. 在 [存储设置] 中，配置本地或WebDAV备份参数。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '4. 在 [基础设置] 中，设置执行周期、重试策略并启用插件。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '5. 点击 [保存] 应用配置。'}]},
                                        ]
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'class': 'mb-2',
                                        },
                                        'content': [
                                            {'component': 'VListItem', 'props': {'prepend-icon': 'mdi-alert-circle-outline'}, 'content': [{'component': 'VListItemTitle', 'text': '【注意事项】'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '- SSH用户需要有在PVE上执行vzdump的权限，建议使用root用户。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '- 如使用SSH密钥认证，请确保MoviePilot有权限读取私钥文件。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '- 备份文件可能占用较大空间，请确保本地和远程存储空间充足。'}]},
                                            {'component': 'VListItem', 'props': {'density': 'compact'}, 'content': [{'component': 'VListItemSubtitle', 'text': '- "立即运行一次" 会在点击保存后约3秒执行，请留意日志输出。'}]},
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础设置卡片（独立显示）
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': basic_settings
                    },
                    # 选项卡卡片
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat'},
                        'content': [
                            {
                                'component': 'VTabs',
                                'props': {'model': 'tab', 'grow': True},
                                'content': [
                                    {'component': 'VTab', 'props': {'value': key, 'prepend-icon': value['icon']}, 'text': value['title']}
                                    for key, value in tabs.items()
                                ]
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VWindow',
                                        'props': {'model': 'tab'},
                                        'content': [
                                            {
                                                'component': 'VWindowItem',
                                                'props': {'value': key},
                                                'content': value['content']
                                            }
                                            for key, value in tabs.items()
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "tab": "connection",
            "enabled": current_config.get("enabled", False),
            "notify": current_config.get("notify", False),
            "cron": current_config.get("cron", "0 3 * * *"),
            "onlyonce": current_config.get("onlyonce", False),
            "retry_count": current_config.get("retry_count", 0),
            "retry_interval": current_config.get("retry_interval", 60),
            "notification_message_type": current_config.get("notification_message_type", "Plugin"),  # 新增
            
            # SSH配置
            "pve_host": current_config.get("pve_host", ""),
            "ssh_port": current_config.get("ssh_port", 22),
            "ssh_username": current_config.get("ssh_username", "root"),
            "ssh_password": current_config.get("ssh_password", ""),
            "ssh_key_file": current_config.get("ssh_key_file", ""),
            
            # 备份配置
            "storage_name": current_config.get("storage_name", "local"),
            "backup_vmid": current_config.get("backup_vmid", ""),
            "enable_local_backup": current_config.get("enable_local_backup", True),
            "backup_path": current_config.get("backup_path", ""),
            "keep_backup_num": current_config.get("keep_backup_num", 7),
            "backup_mode": current_config.get("backup_mode", "snapshot"),
            "compress_mode": current_config.get("compress_mode", "zstd"),
            "auto_delete_after_download": current_config.get("auto_delete_after_download", False),
            "download_all_backups": current_config.get("download_all_backups", False),
            
            # WebDAV配置
            "enable_webdav": current_config.get("enable_webdav", False),
            "webdav_url": current_config.get("webdav_url", ""),
            "webdav_username": current_config.get("webdav_username", ""),
            "webdav_password": current_config.get("webdav_password", ""),
            "webdav_path": current_config.get("webdav_path", ""),
            "webdav_keep_backup_num": current_config.get("webdav_keep_backup_num", 7),
            "clear_history": current_config.get("clear_history", False),
            
            # 恢复配置
            "enable_restore": current_config.get("enable_restore", False),
            "restore_storage": current_config.get("restore_storage", "local"),
            "restore_vmid": current_config.get("restore_vmid", ""),
            "restore_force": current_config.get("restore_force", False),
            "restore_skip_existing": current_config.get("restore_skip_existing", True),
            "restore_file": current_config.get("restore_file", ""),
            "restore_now": current_config.get("restore_now", False),
        }

    def get_page(self) -> List[dict]:
        backup_history_data = self._load_backup_history()
        restore_history_data = self._load_restore_history()
        
        # 合并和排序历史记录
        all_history = []
        for item in backup_history_data:
            item['type'] = '备份'
            all_history.append(item)
        for item in restore_history_data:
            item['type'] = '恢复'
            all_history.append(item)
        
        all_history.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        
        # 获取可用的备份文件
        available_backups = self._get_available_backups()
        local_backups_count = sum(1 for b in available_backups if b['source'] == '本地备份')
        webdav_backups_count = sum(1 for b in available_backups if b['source'] == 'WebDAV备份')
        
        # 获取PVE端任务状态
        pve_backup_status = "未知"
        pve_restore_status = "未知"
        pve_running_tasks = []
        
        if self._pve_host and self._ssh_username and (self._ssh_password or self._ssh_key_file):
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                if self._ssh_key_file:
                    private_key = paramiko.RSAKey.from_private_key_file(self._ssh_key_file)
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, pkey=private_key)
                else:
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, password=self._ssh_password)
                
                # 检查备份任务状态
                check_backup_cmd = "ps aux | grep vzdump | grep -v grep"
                stdin, stdout, stderr = ssh.exec_command(check_backup_cmd)
                running_backups = stdout.read().decode().strip()
                
                if running_backups:
                    pve_backup_status = "运行中"
                    # 解析运行中的备份任务
                    for line in running_backups.split('\n'):
                        if line.strip():
                            pve_running_tasks.append(line.strip())
                else:
                    pve_backup_status = "空闲"
                
                # 检查恢复任务状态
                check_restore_cmd = "ps aux | grep qmrestore | grep -v grep"
                stdin, stdout, stderr = ssh.exec_command(check_restore_cmd)
                running_restores = stdout.read().decode().strip()
                
                if running_restores:
                    pve_restore_status = "运行中"
                    # 解析运行中的恢复任务
                    for line in running_restores.split('\n'):
                        if line.strip():
                            pve_running_tasks.append(line.strip())
                else:
                    pve_restore_status = "空闲"

                ssh.close()
            except Exception as e:
                pve_backup_status = f"连接失败"
                pve_restore_status = f"连接失败"
        
        page_content = []
        
        # 确定显示状态和颜色
        backup_display_status = self._backup_activity if self._backup_activity != "空闲" else pve_backup_status
        restore_display_status = self._restore_activity if self._restore_activity != "空闲" else pve_restore_status

        if backup_display_status == "空闲":
            backup_status_color = "success"
        elif "失败" in backup_display_status:
            backup_status_color = "error"
        else:
            backup_status_color = "warning"

        if restore_display_status == "空闲":
            restore_status_color = "success"
        elif "失败" in restore_display_status:
            restore_status_color = "error"
        else:
            restore_status_color = "warning"

        # PVE状态卡片
        page_content.append({
            'component': 'VCard',
            'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {
                    'component': 'VCardTitle',
                    'props': {'class': 'text-h6'},
                    'text': '🔍 任务状态'
                },
                {
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'VRow',
                            'props': {'align': 'center', 'no-gutters': True},
                            'content': [
                                {'component': 'VCol', 'props': {'cols': 'auto'}, 'content': [
                                    {'component': 'VChip', 'props': {
                                        'color': backup_status_color,
                                        'variant': 'elevated',
                                        'label': True,
                                        'prepend_icon': 'mdi-content-save'
                                    }, 'text': f"备份状态: {backup_display_status}"}
                                ]},
                                {'component': 'VCol', 'props': {'cols': 'auto', 'class': 'ml-2'}, 'content': [
                                    {'component': 'VChip', 'props': {
                                        'color': restore_status_color,
                                        'variant': 'elevated',
                                        'label': True,
                                        'prepend_icon': 'mdi-restore'
                                    }, 'text': f"恢复状态: {restore_display_status}"}
                                ]},
                                *([{'component': 'VCol', 'props': {'cols': 'auto', 'class': 'ml-4'}, 'content': [
                                    {'component': 'VChip', 'props': {
                                        'color': 'info',
                                        'variant': 'outlined',
                                        'label': True,
                                        'prepend_icon': 'mdi-harddisk'
                                    }, 'text': f"本地备份: {local_backups_count} 个"}
                                ]}] if self._enable_local_backup else []),
                                *([{'component': 'VCol', 'props': {'cols': 'auto', 'class': 'ml-2'}, 'content': [
                                    {'component': 'VChip', 'props': {
                                        'color': 'info',
                                        'variant': 'outlined',
                                        'label': True,
                                        'prepend_icon': 'mdi-cloud-outline'
                                    }, 'text': f"WebDAV备份: {webdav_backups_count} 个"}
                                ]}] if self._enable_webdav else []),
                                {'component': 'VSpacer'},
                                {'component': 'VCol', 'props': {'cols': 'auto'}, 'content': [
                                    {'component': 'div', 'props': {'class': 'd-flex align-center text-h6'}, 'content':[
                                        {'component': 'VIcon', 'props': {'icon': 'mdi-server', 'size': 'large', 'class': 'mr-2'}},
                                        {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': f"🖥️ PVE 主机: {self._pve_host or '未配置'}"},
                                    ]}
                                ]},
                            ]
                        }
                    ]
                }
            ]
        })
        
        # 如果有运行中的任务，显示详细信息
        if pve_running_tasks:
            page_content.append({
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {'class': 'text-h6'},
                        'text': '⚡ 正在运行的任务'
                    },
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'warning',
                                    'variant': 'tonal',
                                    'class': 'mb-2'
                                },
                                'text': '检测到PVE端有任务正在运行，插件将等待任务完成后再次尝试。'
                            }
                        ] + [
                            {
                                'component': 'VListItem',
                                'props': {'density': 'compact'},
                                'content': [{'component': 'VListItemSubtitle', 'text': task}]
                            }
                            for task in pve_running_tasks
                        ]
                    }
                ]
            })
        
        # 统一的历史记录卡片
        if not all_history:
            page_content.append({
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': '暂无任务历史记录。',
                    'class': 'mb-2'
                }
            })
        else:
            history_rows = []
            for item in all_history:
                timestamp_str = datetime.fromtimestamp(item.get("timestamp", 0)).strftime('%Y-%m-%d %H:%M:%S') if item.get("timestamp") else "N/A"
                item_type = item.get("type", "未知")
                type_color = "primary" if item_type == "备份" else "accent"
                
                status_success = item.get("success", False)
                status_text = "成功" if status_success else "失败"
                status_color = "success" if status_success else "error"
                
                filename_str = item.get("filename", "N/A")
                message_str = item.get("message", "")
                
                details_str = filename_str
                if item_type == '恢复':
                    target_vmid = item.get('target_vmid', 'N/A')
                    details_str = f"{filename_str} ➜ {target_vmid}"
                elif item_type == '备份':
                    # 从消息中提取VMID信息
                    vmid_match = re.search(r'\[VMID: (.*?)\]', message_str)
                    if vmid_match:
                        vmids = vmid_match.group(1)
                        details_str = f"{filename_str} [{vmids}]"
                        # 移除消息中的VMID信息，避免重复显示
                        message_str = message_str.replace(f" [VMID: {vmids}]", "")
                
                history_rows.append({
                    'component': 'tr',
                    'content': [
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': timestamp_str},
                        {'component': 'td', 'content': [
                            {'component': 'VChip', 'props': {'color': type_color, 'size': 'small', 'variant': 'flat'}, 'text': item_type}
                        ]},
                        {'component': 'td', 'content': [
                            {'component': 'VChip', 'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'}, 'text': status_text}
                        ]},
                        {'component': 'td', 'text': details_str},
                        {'component': 'td', 'text': message_str},
                    ]
                })

            page_content.append({
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-h6"},
                        "text": "📊 任务历史"
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "props": {
                                    "hover": True,
                                    "density": "compact"
                                },
                                "content": [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [
                                                    {'component': 'th', 'text': '时间'},
                                                    {'component': 'th', 'text': '类型'},
                                                    {'component': 'th', 'text': '状态'},
                                                    {'component': 'th', 'text': '详情'},
                                                    {'component': 'th', 'text': '消息'}
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': history_rows
                                    }
                                ]
                            }
                        ]
                    }
                ]
            })
        
        return page_content

    def stop_service(self):
        """完全停止服务并清理资源"""
        try:
            # 1. 等待当前任务完成
            if self._lock and hasattr(self._lock, 'locked') and self._lock.locked():
                logger.info(f"等待 {self.plugin_name} 当前任务执行完成...")
                acquired = self._lock.acquire(timeout=300)
                if acquired:
                    self._lock.release()
                else:
                    logger.warning(f"{self.plugin_name} 等待任务超时。")
            
            # 等待恢复任务完成
            if self._restore_lock and hasattr(self._restore_lock, 'locked') and self._restore_lock.locked():
                logger.info(f"等待 {self.plugin_name} 当前恢复任务执行完成...")
                acquired = self._restore_lock.acquire(timeout=300)
                if acquired:
                    self._restore_lock.release()
                else:
                    logger.warning(f"{self.plugin_name} 等待恢复任务超时。")
            
            # 等待全局任务锁释放
            if self._global_task_lock and hasattr(self._global_task_lock, 'locked') and self._global_task_lock.locked():
                logger.info(f"等待 {self.plugin_name} 全局任务锁释放...")
                acquired = self._global_task_lock.acquire(timeout=300)
                if acquired:
                    self._global_task_lock.release()
                else:
                    logger.warning(f"{self.plugin_name} 等待全局任务锁超时。")
            
            # 2. 停止调度器
            if self._scheduler:
                try:
                    # 移除所有任务
                    self._scheduler.remove_all_jobs()
                    # 关闭调度器
                    if self._scheduler.running:
                        self._scheduler.shutdown(wait=True)
                    self._scheduler = None
                except Exception as e:
                    logger.error(f"停止调度器时出错: {str(e)}")
            
            # 3. 重置状态
            self._running = False
            if not self._stopped:
                logger.info(f"{self.plugin_name} 服务已完全停止。")
                self._stopped = True
            
            # 4. 清理配置哈希（当插件被禁用时）
            if not self._enabled:
                self._last_config_hash = None
                self.save_data('last_config_hash', None)
                logger.debug(f"{self.plugin_name} 已清理配置哈希")
            
        except Exception as e:
            logger.error(f"{self.plugin_name} 退出插件失败：{str(e)}")

    def run_backup_job(self):
        """执行备份任务"""
        # 如果已有任务在运行,直接返回
        if not self._lock:
            self._lock = threading.Lock()
        if not self._global_task_lock:
            self._global_task_lock = threading.Lock()
            
        # 检查是否有恢复任务正在执行（恢复任务优先级更高）
        if self._restore_lock and hasattr(self._restore_lock, 'locked') and self._restore_lock.locked():
            logger.info(f"{self.plugin_name} 检测到恢复任务正在执行，备份任务跳过（恢复任务优先级更高）！")
            return
            
        # 尝试获取全局任务锁，如果获取不到说明有其他任务在运行
        if not self._global_task_lock.acquire(blocking=False):
            logger.debug(f"{self.plugin_name} 检测到其他任务正在执行，备份任务跳过！")
            return
            
        # 尝试获取备份锁，如果获取不到说明有备份任务在运行
        if not self._lock.acquire(blocking=False):
            logger.debug(f"{self.plugin_name} 已有备份任务正在执行，本次调度跳过！")
            self._global_task_lock.release()  # 释放全局锁
            return
            
        history_entry = {
            "timestamp": time.time(),
            "success": False,
            "filename": None,
            "message": "任务开始"
        }
        self._backup_activity = "任务开始"
            
        try:
            self._running = True
            logger.info(f"开始执行 {self.plugin_name} 任务...")

            if not self._pve_host or not self._ssh_username or (not self._ssh_password and not self._ssh_key_file):
                error_msg = "配置不完整：PVE主机地址、SSH用户名或SSH认证信息(密码/密钥)未设置。"
                logger.error(f"{self.plugin_name} {error_msg}")
                self._send_notification(success=False, message=error_msg, backup_details={})
                history_entry["message"] = error_msg
                self._save_backup_history_entry(history_entry)
                return

            if not self._backup_path:
                error_msg = "备份路径未配置且无法设置默认路径。"
                logger.error(f"{self.plugin_name} {error_msg}")
                self._send_notification(success=False, message=error_msg, backup_details={})
                history_entry["message"] = error_msg
                self._save_backup_history_entry(history_entry)
                return

            try:
                Path(self._backup_path).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                error_msg = f"创建本地备份目录 {self._backup_path} 失败: {e}"
                logger.error(f"{self.plugin_name} {error_msg}")
                self._send_notification(success=False, message=error_msg, backup_details={})
                history_entry["message"] = error_msg
                self._save_backup_history_entry(history_entry)
                return
            
            success_final = False
            error_msg_final = "未知错误"
            downloaded_file_final = None
            backup_details_final = {}
            
            for i in range(self._retry_count + 1):
                logger.info(f"{self.plugin_name} 开始第 {i+1}/{self._retry_count +1} 次备份尝试...")
                current_try_success, current_try_error_msg, current_try_downloaded_file, current_try_backup_details = self._perform_backup_once()
                
                if current_try_success:
                    success_final = True
                    downloaded_file_final = current_try_downloaded_file
                    backup_details_final = current_try_backup_details
                    error_msg_final = None
                    logger.info(f"{self.plugin_name} 第{i+1}次尝试成功。备份文件: {downloaded_file_final}")
                    break 
                else:
                    error_msg_final = current_try_error_msg
                    logger.warning(f"{self.plugin_name} 第{i+1}次备份尝试失败: {error_msg_final}")
                    if i < self._retry_count:
                        logger.info(f"{self._retry_interval}秒后重试...")
                        time.sleep(self._retry_interval)
                    else:
                        logger.error(f"{self.plugin_name} 所有 {self._retry_count +1} 次尝试均失败。最后错误: {error_msg_final}")
            
            # 只在所有尝试都失败时保存一条失败历史
            if not success_final:
                history_entry["success"] = False
                history_entry["filename"] = None
                history_entry["message"] = f"备份失败: {error_msg_final}"
                self._save_backup_history_entry(history_entry)
            
            self._send_notification(success=success_final, message="备份成功" if success_final else f"备份失败: {error_msg_final}", filename=downloaded_file_final, backup_details=backup_details_final)
                
        except Exception as e:
            logger.error(f"{self.plugin_name} 任务执行主流程出错：{str(e)}")
            history_entry["message"] = f"任务执行主流程出错: {str(e)}"
            self._send_notification(success=False, message=history_entry["message"], backup_details={})
            self._save_backup_history_entry(history_entry)
        finally:
            self._running = False
            self._backup_activity = "空闲"
            # 不再在finally里保存合并历史
            if self._lock and hasattr(self._lock, 'locked') and self._lock.locked():
                try:
                    self._lock.release()
                except RuntimeError:
                    pass
            if self._global_task_lock and hasattr(self._global_task_lock, 'locked') and self._global_task_lock.locked():
                try:
                    self._global_task_lock.release()
                except RuntimeError:
                    pass
            logger.info(f"{self.plugin_name} 任务执行完成。")

    def _perform_backup_once(self) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        """
        执行一次备份操作
        :return: (是否成功, 错误消息, 备份文件名, 备份详情)
        """
        if not self._pve_host:
            return False, "未配置PVE主机地址", None, {}

        # 创建SSH客户端
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sftp = None
        
        try:
            # 尝试SSH连接
            try:
                if self._ssh_key_file:
                    # 使用密钥认证
                    private_key = paramiko.RSAKey.from_private_key_file(self._ssh_key_file)
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, pkey=private_key)
                else:
                    # 使用密码认证
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, password=self._ssh_password)
                logger.info(f"{self.plugin_name} SSH连接成功")
            except Exception as e:
                return False, f"SSH连接失败: {str(e)}", None, {}

            # 1. 创建备份
            logger.info(f"{self.plugin_name} 开始创建备份...")
            
            # 检查PVE端是否有正在运行的备份任务
            check_running_cmd = "ps aux | grep vzdump | grep -v grep"
            stdin, stdout, stderr = ssh.exec_command(check_running_cmd)
            running_backups = stdout.read().decode().strip()
            
            if running_backups:
                logger.warning(f"{self.plugin_name}  logger.warning ")
                logger.info(f"{self.plugin_name} 正在运行的备份进程: {running_backups}")
                return False, "PVE端已有备份任务在运行，为避免冲突跳过本次备份", None, {}
            
            # 检查是否指定了要备份的容器ID
            if not self._backup_vmid or self._backup_vmid.strip() == "":
                # 如果没有指定容器ID，尝试获取所有可用的容器
                logger.info(f"{self.plugin_name} 未指定容器ID，尝试获取所有可用的容器...")
                list_cmd = "qm list | grep -E '^[0-9]+' | awk '{print $1}' | tr '\n' ',' | sed 's/,$//'"
                stdin, stdout, stderr = ssh.exec_command(list_cmd)
                available_vmids = stdout.read().decode().strip()
                
                if not available_vmids:
                    # 如果还是没有找到，尝试获取所有LXC容器
                    list_cmd = "pct list | grep -E '^[0-9]+' | awk '{print $1}' | tr '\n' ',' | sed 's/,$//'"
                    stdin, stdout, stderr = ssh.exec_command(list_cmd)
                    available_vmids = stdout.read().decode().strip()
                
                if not available_vmids:
                    return False, "未找到任何可用的虚拟机或容器，请检查PVE主机状态或手动指定容器ID", None, {}
                
                self._backup_vmid = available_vmids
                logger.info(f"{self.plugin_name} 自动获取到容器ID: {self._backup_vmid}")
            
            # 构建vzdump命令
            backup_cmd = f"vzdump {self._backup_vmid} "
            backup_cmd += f"--compress {self._compress_mode} "
            backup_cmd += f"--mode {self._backup_mode} "
            backup_cmd += f"--storage {self._storage_name} "
            
            # 执行备份命令
            logger.info(f"{self.plugin_name} 执行命令: {backup_cmd}")
            stdin, stdout, stderr = ssh.exec_command(backup_cmd)
    
            created_backup_files = []
            # 实时输出vzdump日志
            while True:
                line = stdout.readline()
                if not line:
                    break
                line = line.strip()
                logger.info(f"{self.plugin_name} vzdump输出: {line}")
                # 从vzdump日志中解析出备份文件名
                match = re.search(r"creating vzdump archive '(.+)'", line)
                if match:
                    filepath = match.group(1)
                    logger.info(f"{self.plugin_name} 从日志中检测到备份文件: {filepath}")
                    created_backup_files.append(filepath)
            
            # 等待命令完成
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_output = stderr.read().decode().strip()
                
                # 检查是否是手动暂停或中断的情况
                if "got unexpected control message" in error_output or exit_status == -1:
                    # 检查PVE端是否有正在运行的备份任务
                    check_backup_cmd = "ps aux | grep vzdump | grep -v grep"
                    stdin, stdout, stderr = ssh.exec_command(check_backup_cmd)
                    running_backups = stdout.read().decode().strip()
                    
                    if running_backups:
                        error_msg = f"备份任务被手动暂停或中断。检测到PVE端仍有备份进程在运行，可能是您在PVE界面手动暂停了备份任务。"
                        logger.warning(f"{self.plugin_name} {error_msg}")
                        logger.info(f"{self.plugin_name} 检测到的备份进程: {running_backups}")
                    else:
                        error_msg = f"备份任务被中断。SSH连接出现意外控制消息，可能是网络问题或PVE端任务被强制终止。"
                        logger.warning(f"{self.plugin_name} {error_msg}")
                    
                    return False, error_msg, None, {}
                else:
                    # 其他类型的错误
                    return False, f"备份创建失败: {error_output}", None, {}

            if not created_backup_files:
                return False, "未能从vzdump日志中解析出备份文件名, 无法进行下载。", None, {}

            files_to_download = []
            if self._download_all_backups:
                files_to_download = created_backup_files
            elif created_backup_files:
                # 仅下载最后一个，即最新的
                files_to_download.append(created_backup_files[-1])

            if not files_to_download:
                return False, "没有找到需要下载的备份文件。", None, {}
            
            logger.info(f"{self.plugin_name} 准备下载 {len(files_to_download)} 个文件: {', '.join(files_to_download)}")

            sftp = ssh.open_sftp()
            
            all_downloads_successful = True
            downloaded_files_info = []
            filenames = []
            vmids = []

            for remote_file_path in files_to_download:
                success, error_msg, filename, details = self._download_single_backup_file(ssh, sftp, remote_file_path, os.path.basename(remote_file_path))
                if success:
                    downloaded_files_info.append({
                        "filename": filename,
                        "details": details
                    })
                    filenames.append(filename)
                    # 提取VMID
                    vmid = self._extract_vmid_from_backup(filename)
                    if vmid:
                        vmids.append(vmid)
                else:
                    all_downloads_successful = False
                    logger.error(f"{self.plugin_name} 处理文件 {remote_file_path} 失败: {error_msg}")

            # --- 所有文件处理完成后，统一执行清理 ---
            if self._enable_local_backup:
                self._cleanup_old_backups()
            if self._enable_webdav and self._webdav_url:
                logger.info(f"{self.plugin_name} 开始清理WebDAV旧备份...")
                self._cleanup_webdav_backups()

            # 合并历史记录逻辑
            if downloaded_files_info:
                # 成功时保存一条合并历史
                history_entry = {
                    "timestamp": time.time(),
                    "success": True,
                    "filename": ", ".join(filenames),
                    "message": f"备份成功 [VMID: {', '.join(vmids)}]"
                }
                self._save_backup_history_entry(history_entry)
                # 返回最后一个成功下载的文件信息
                last_file = downloaded_files_info[-1]
                return True, None, last_file["filename"], {
                    "downloaded_files": downloaded_files_info,
                    "last_file_details": last_file["details"]
                }
            else:
                # 失败时只保存一条失败历史
                history_entry = {
                    "timestamp": time.time(),
                    "success": False,
                    "filename": None,
                    "message": "所有备份文件下载失败，详情请查看日志"
                }
                self._save_backup_history_entry(history_entry)
                return False, "所有备份文件下载失败，详情请查看日志", None, {}

        except Exception as e:
            error_msg = f"备份过程中发生错误: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg, None, {}
            
        finally:
            # 确保关闭SFTP和SSH连接
            if sftp:
                try:
                    sftp.close()
                except:
                    pass
            if ssh:
                try:
                    ssh.close()
                except:
                    pass

    def _cleanup_old_backups(self):
        if not self._backup_path or self._keep_backup_num <= 0: return
        try:
            logger.info(f"{self.plugin_name} 开始清理本地备份目录: {self._backup_path}, 保留数量: {self._keep_backup_num} (仅处理 Proxmox 备份文件 .tar.gz/.tar.lzo/.tar.zst/.vma.gz/.vma.lzo/.vma.zst)")
            backup_dir = Path(self._backup_path)
            if not backup_dir.is_dir():
                logger.warning(f"{self.plugin_name} 本地备份目录 {self._backup_path} 不存在，无需清理。")
                return

            files = []
            for f_path_obj in backup_dir.iterdir():
                if f_path_obj.is_file() and (
                    f_path_obj.name.endswith('.tar.gz') or 
                    f_path_obj.name.endswith('.tar.lzo') or 
                    f_path_obj.name.endswith('.tar.zst') or
                    f_path_obj.name.endswith('.vma.gz') or 
                    f_path_obj.name.endswith('.vma.lzo') or 
                    f_path_obj.name.endswith('.vma.zst')
                ):
                    try:
                        match = re.search(r'(\d{4}\d{2}\d{2}[_]?\d{2}\d{2}\d{2})', f_path_obj.stem)
                        file_time = None
                        if match:
                            time_str = match.group(1).replace('_','')
                            try:
                                file_time = datetime.strptime(time_str, '%Y%m%d%H%M%S').timestamp()
                            except ValueError:
                                pass 
                        if file_time is None:
                           file_time = f_path_obj.stat().st_mtime
                        files.append({'path': f_path_obj, 'name': f_path_obj.name, 'time': file_time})
                    except Exception as e:
                        logger.error(f"{self.plugin_name} 处理文件 {f_path_obj.name} 时出错: {e}")
                        try:
                            files.append({'path': f_path_obj, 'name': f_path_obj.name, 'time': f_path_obj.stat().st_mtime})
                        except Exception as stat_e:
                            logger.error(f"{self.plugin_name} 无法获取文件状态 {f_path_obj.name}: {stat_e}")

            files.sort(key=lambda x: x['time'], reverse=True)
            
            if len(files) > self._keep_backup_num:
                files_to_delete = files[self._keep_backup_num:]
                logger.info(f"{self.plugin_name} 找到 {len(files_to_delete)} 个旧 Proxmox 备份文件需要删除。")
                for f_info in files_to_delete:
                    try:
                        f_info['path'].unlink()
                        logger.info(f"{self.plugin_name} 已删除旧备份文件: {f_info['name']}")
                    except OSError as e:
                        logger.error(f"{self.plugin_name} 删除旧备份文件 {f_info['name']} 失败: {e}")
            else:
                logger.info(f"{self.plugin_name} 当前 Proxmox 备份文件数量 ({len(files)}) 未超过保留限制 ({self._keep_backup_num})，无需清理。")
        except Exception as e:
            logger.error(f"{self.plugin_name} 清理旧备份文件时发生错误: {e}")

    def _create_webdav_directories(self, auth, base_url: str, path: str) -> Tuple[bool, Optional[str]]:
        """递归创建WebDAV目录"""
        try:
            import requests
            from urllib.parse import urljoin, urlparse

            # 检测是否为Alist服务器（端口5244）
            parsed_url = urlparse(base_url)
            is_alist = parsed_url.port == 5244 or '5244' in base_url

            # 分割路径
            path_parts = [p for p in path.split('/') if p]
            current_path = base_url.rstrip('/')

            # 如果是Alist服务器且base_url不包含/dav,添加dav前缀
            if is_alist and '/dav' not in current_path:
                current_path = f"{current_path}/dav"

            # 逐级创建目录
            for part in path_parts:
                current_path = f"{current_path}/{part}"
                
                # 检查当前目录是否存在
                check_response = requests.request(
                    'PROPFIND',
                    current_path,
                    auth=auth,
                    headers={
                        'Depth': '0',
                        'User-Agent': 'MoviePilot/1.0',
                        'Connection': 'keep-alive'
                    },
                    timeout=10,
                    verify=False
                )

                if check_response.status_code == 404:
                    # 目录不存在，创建它
                    logger.info(f"{self.plugin_name} 创建WebDAV目录: {current_path}")
                    mkdir_response = requests.request(
                        'MKCOL',
                        current_path,
                        auth=auth,
                        headers={
                            'User-Agent': 'MoviePilot/1.0',
                            'Connection': 'keep-alive'
                        },
                        timeout=10,
                        verify=False
                    )
                    
                    if mkdir_response.status_code not in [200, 201, 204]:
                        # 如果是405错误(Method Not Allowed),可能目录已存在
                        if mkdir_response.status_code == 405:
                            logger.warning(f"{self.plugin_name} 目录可能已存在: {current_path}")
                            continue
                        return False, f"创建WebDAV目录失败 {current_path}, 状态码: {mkdir_response.status_code}, 响应: {mkdir_response.text}"
                elif check_response.status_code not in [200, 207]:
                    return False, f"检查WebDAV目录失败 {current_path}, 状态码: {check_response.status_code}, 响应: {check_response.text}"

            return True, None
        except Exception as e:
            return False, f"创建WebDAV目录时发生错误: {str(e)}"

    def _upload_to_webdav(self, local_file_path: str, filename: str) -> Tuple[bool, Optional[str]]:
        """上传文件到WebDAV服务器"""
        if not self._enable_webdav or not self._webdav_url:
            return False, "WebDAV未启用或URL未配置"

        try:
            import requests
            from urllib.parse import urljoin, urlparse
            import base64
            from requests.auth import HTTPBasicAuth, HTTPDigestAuth
            import socket

            # 验证WebDAV URL格式
            parsed_url = urlparse(self._webdav_url)
            if not parsed_url.scheme or not parsed_url.netloc:
                return False, f"WebDAV URL格式无效: {self._webdav_url}"

            # 检查服务器连接
            try:
                host = parsed_url.netloc.split(':')[0]
                port = int(parsed_url.port or (443 if parsed_url.scheme == 'https' else 80))
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((host, port))
                sock.close()
                if result != 0:
                    return False, f"无法连接到WebDAV服务器 {host}:{port}，请检查服务器地址和端口是否正确"
            except Exception as e:
                return False, f"检查WebDAV服务器连接时出错: {str(e)}"

            # 构建WebDAV基础URL
            base_url = self._webdav_url.rstrip('/')
            webdav_path = self._webdav_path.lstrip('/')
            
            # 检测是否为Alist服务器（端口5244）
            is_alist = parsed_url.port == 5244 or '5244' in self._webdav_url
            
            # 构建可能的上传URL列表
            possible_upload_urls = []
            
            if is_alist:
                # 如果base_url不包含/dav,添加dav前缀
                if '/dav' not in base_url:
                    base_url = f"{base_url}/dav"
                
                # Alist的特殊路径结构
                if webdav_path:
                    possible_upload_urls.extend([
                        f"{base_url}/{webdav_path}/{filename}"      # Alist标准路径
                    ])
                else:
                    possible_upload_urls.extend([
                        f"{base_url}/{filename}"      # Alist标准路径
                    ])
            else:
                # 标准WebDAV路径
                if webdav_path:
                    possible_upload_urls.extend([
                        f"{base_url}/{webdav_path}/{filename}",
                        f"{base_url}/dav/{webdav_path}/{filename}",
                        f"{base_url}/remote.php/webdav/{webdav_path}/{filename}",
                        f"{base_url}/dav/files/{self._webdav_username}/{webdav_path}/{filename}"
                    ])
                else:
                    possible_upload_urls.extend([
                        f"{base_url}/{filename}",
                        f"{base_url}/dav/{filename}",
                        f"{base_url}/remote.php/webdav/{filename}"
                    ])

            # 准备认证信息
            auth_methods = [
                HTTPBasicAuth(self._webdav_username, self._webdav_password),
                HTTPDigestAuth(self._webdav_username, self._webdav_password),
                (self._webdav_username, self._webdav_password)
            ]

            # 设置重试次数和间隔
            max_retries = 3  # 最大重试次数
            retry_interval = 5  # 重试间隔(秒)
            retry_count = 0

            # 首先尝试检查目录是否存在
            auth_success = False
            last_error = None
            successful_auth = None

            for auth in auth_methods:
                try:
                    logger.info(f"{self.plugin_name} 尝试使用认证方式 {type(auth).__name__} 连接WebDAV服务器...")
                    
                    # 测试连接
                    test_response = requests.request(
                        'PROPFIND',
                        base_url,
                        auth=auth,
                        headers={
                            'Depth': '0',
                            'User-Agent': 'MoviePilot/1.0',
                            'Accept': '*/*',
                            'Connection': 'keep-alive'
                        },
                        timeout=30,  # 增加超时时间
                        verify=False
                    )

                    if test_response.status_code in [200, 207]:
                        logger.info(f"{self.plugin_name} WebDAV认证成功，使用认证方式: {type(auth).__name__}")
                        auth_success = True
                        successful_auth = auth
                        break
                    elif test_response.status_code == 401:
                        last_error = f"认证失败，状态码: 401, 响应: {test_response.text}"
                        continue
                    else:
                        last_error = f"检查WebDAV服务器失败，状态码: {test_response.status_code}, 响应: {test_response.text}"
                        continue

                except requests.exceptions.RequestException as e:
                    last_error = f"连接WebDAV服务器失败: {str(e)}"
                    continue

            if not auth_success:
                return False, f"所有认证方式均失败。最后错误: {last_error}"

            # 创建目录结构
            if webdav_path:
                create_success, create_error = self._create_webdav_directories(successful_auth, base_url, webdav_path)
                if not create_success:
                    logger.warning(f"{self.plugin_name} 创建目录失败，但继续尝试上传: {create_error}")

            # 准备上传请求头
            headers = {
                'Content-Type': 'application/octet-stream',
                'User-Agent': 'MoviePilot/1.0',
                'Accept': '*/*',
                'Connection': 'keep-alive'
            }

            # 尝试多种上传方法
            upload_methods = [
                ('PUT', headers),
                ('PUT', {**headers, 'Content-Type': 'application/x-tar'}),
                ('PUT', {**headers, 'Overwrite': 'T'}),
                ('POST', headers),
                ('POST', {**headers, 'Content-Type': 'application/x-tar'}),
                ('POST', {**headers, 'Overwrite': 'T'})
            ]

            # 获取文件大小
            file_size = os.path.getsize(local_file_path)
            chunk_size = 8192 * 1024

            # 尝试每个URL和每种方法
            for upload_url in possible_upload_urls:
                logger.info(f"{self.plugin_name} 尝试上传到URL: {upload_url}")
                
                for method, method_headers in upload_methods:
                    retry_count = 0
                    while retry_count <= max_retries:
                        try:
                            if retry_count > 0:
                                logger.info(f"{self.plugin_name} 第{retry_count}次重试上传...")
                                time.sleep(retry_interval)
                            
                            logger.info(f"{self.plugin_name} 尝试使用 {method} 方法上传到WebDAV...")
                            
                            # 使用requests的data参数流式上传
                            with open(local_file_path, 'rb') as f:
                                uploaded_size = 0
                                last_progress = -1  # 记录上次显示的进度
                                last_activity_time = time.time()  # 记录最后活动时间
                                
                                def upload_callback():
                                    nonlocal uploaded_size, last_progress, last_activity_time
                                    while True:
                                        chunk = f.read(chunk_size)
                                        if not chunk:
                                            break
                                        uploaded_size += len(chunk)
                                        current_time = time.time()
                                        
                                        # 检查是否超过30秒没有进度更新
                                        if current_time - last_activity_time > 30:
                                            logger.warning(f"{self.plugin_name} 上传可能停滞，已有30秒没有进度更新")
                                        
                                        # 更新最后活动时间
                                        last_activity_time = current_time
                                        
                                        # 计算进度
                                        if file_size > 0:
                                            progress = (uploaded_size / file_size) * 100
                                            # 每10%显示一次进度
                                            current_progress = int(progress / 10) * 10
                                            if current_progress > last_progress:
                                                self._backup_activity = f"上传WebDAV中: {progress:.1f}%"
                                                logger.info(f"{self.plugin_name} WebDAV上传进度: {progress:.1f}%")
                                                last_progress = current_progress
                                        yield chunk
                                
                                # 设置请求超时
                                timeout = max(300, int(file_size / (1024 * 1024) * 2))  # 根据文件大小动态调整超时时间,最少5分钟
                                
                                if method == 'PUT':
                                    response = requests.put(
                                        upload_url,
                                        data=upload_callback(),
                                        auth=successful_auth,
                                        headers=method_headers,
                                        timeout=timeout,
                                        verify=False
                                    )
                                else:  # POST
                                    response = requests.post(
                                        upload_url,
                                        data=upload_callback(),
                                        auth=successful_auth,
                                        headers=method_headers,
                                        timeout=timeout,
                                        verify=False
                                    )

                            if response.status_code in [200, 201, 204]:
                                logger.info(f"{self.plugin_name} 成功使用 {method} 方法上传文件到WebDAV: {upload_url}")
                                return True, None
                            elif response.status_code == 405:
                                logger.warning(f"{self.plugin_name} {method} 方法不被支持，状态码: 405")
                                break  # 直接尝试下一种方法
                            elif response.status_code == 404:
                                logger.warning(f"{self.plugin_name} URL不存在，状态码: 404 - {upload_url}")
                                break  # 这个URL不存在，尝试下一个URL
                            elif response.status_code == 409:
                                # 文件冲突，这是WebDAV标准中的常见问题
                                logger.warning(f"{self.plugin_name} WebDAV文件冲突(409)，尝试使用Overwrite头: {upload_url}")
                                # 添加Overwrite头并重试
                                method_headers['Overwrite'] = 'T'
                                continue
                            elif response.status_code == 507:
                                logger.error(f"{self.plugin_name} WebDAV服务器存储空间不足，状态码: 507")
                                return False, "WebDAV服务器存储空间不足"
                            else:
                                error_msg = f"{method} 方法上传失败，状态码: {response.status_code}, 响应: {response.text}"
                                logger.warning(f"{self.plugin_name} {error_msg}")
                                if retry_count < max_retries:
                                    retry_count += 1
                                    continue
                                break  # 达到最大重试次数，尝试下一种方法

                        except requests.exceptions.Timeout:
                            error_msg = "上传请求超时"
                            logger.warning(f"{self.plugin_name} {error_msg}")
                            if retry_count < max_retries:
                                retry_count += 1
                                continue
                            break
                            
                        except requests.exceptions.RequestException as e:
                            error_msg = f"上传请求失败: {str(e)}"
                            logger.warning(f"{self.plugin_name} {error_msg}")
                            if retry_count < max_retries:
                                retry_count += 1
                                continue
                            break

            # 所有URL和方法都失败了
            error_msg = f"WebDAV上传失败：所有上传URL和方法均失败。\n\n尝试的URL:\n" + "\n".join([f"- {url}" for url in possible_upload_urls]) + f"\n\n可能的原因：\n1. WebDAV服务器不支持PUT/POST方法\n2. 服务器配置不允许文件上传\n3. 认证信息不正确或权限不足\n4. 服务器需要特定的请求头或协议版本\n5. URL路径构建不正确\n\n建议：\n1. 检查WebDAV服务器配置，确保支持PUT方法\n2. 验证用户权限，确保有写入权限\n3. 尝试使用其他WebDAV客户端测试\n4. 联系WebDAV服务提供商确认支持的功能\n5. 检查WebDAV路径配置是否正确"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg

        except Exception as e:
            error_msg = f"WebDAV上传过程中发生错误: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg

    def _cleanup_webdav_backups(self):
        """清理WebDAV上的旧备份文件"""
        if not self._enable_webdav or not self._webdav_url or self._webdav_keep_backup_num <= 0:
            return

        try:
            import requests
            from urllib.parse import urljoin, quote, urlparse
            from xml.etree import ElementTree

            # 构建WebDAV基础URL
            base_url = self._webdav_url.rstrip('/')
            webdav_path = self._webdav_path.lstrip('/')
            
            # 检测是否为Alist服务器（端口5244）
            parsed_url = urlparse(self._webdav_url)
            is_alist = parsed_url.port == 5244 or '5244' in self._webdav_url
            
            # 构建可能的URL列表
            possible_urls = []
            if is_alist:
                # Alist的特殊路径结构
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/dav/{webdav_path}",      # Alist标准路径
                        f"{base_url}/{webdav_path}"           # 直接路径
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}/dav",      # Alist标准路径
                        f"{base_url}"           # 直接路径
                    ])
            else:
                # 标准WebDAV路径
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/{webdav_path}",
                        f"{base_url}/dav/{webdav_path}",
                        f"{base_url}/remote.php/webdav/{webdav_path}",
                        f"{base_url}/dav/files/{self._webdav_username}/{webdav_path}"
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}",
                        f"{base_url}/dav",
                        f"{base_url}/remote.php/webdav"
                    ])
            
            # 尝试不同的URL结构
            working_url = None
            for test_url in possible_urls:
                try:
                    response = requests.request(
                        'PROPFIND',
                        test_url,
                        auth=(self._webdav_username, self._webdav_password),
                        headers={
                            'Depth': '1',
                            'Content-Type': 'application/xml',
                            'Accept': '*/*',
                            'User-Agent': 'MoviePilot/1.0'
                        },
                        timeout=10,
                        verify=False
                    )
                    if response.status_code == 207:
                        working_url = test_url
                        logger.info(f"{self.plugin_name} 找到可用的WebDAV清理URL: {working_url}")
                        break
                except Exception as e:
                    logger.debug(f"{self.plugin_name} 测试WebDAV清理URL失败: {test_url}, 错误: {e}")
                    continue
            
            if not working_url:
                logger.warning(f"{self.plugin_name} 无法找到可用的WebDAV清理URL，跳过清理")
                return
            
            # 发送PROPFIND请求获取文件列表
            headers = {
                'Depth': '1',
                'Content-Type': 'application/xml',
                'Accept': '*/*',
                'User-Agent': 'MoviePilot/1.0'
            }
            
            response = requests.request(
                'PROPFIND',
                working_url,
                auth=(self._webdav_username, self._webdav_password),
                headers=headers,
                timeout=30,
                verify=False
            )

            if response.status_code != 207:
                logger.error(f"{self.plugin_name} 获取WebDAV文件列表失败，状态码: {response.status_code}")
                return

            # 解析XML响应
            try:
                root = ElementTree.fromstring(response.content)
            except ElementTree.ParseError as e:
                logger.error(f"{self.plugin_name} 解析WebDAV响应XML失败: {str(e)}")
                return

            files = []

            # 遍历所有文件
            for response in root.findall('.//{DAV:}response'):
                href = response.find('.//{DAV:}href')
                if href is None or not href.text:
                    continue

                file_path = href.text
                # 只处理Proxmox备份文件
                if not (file_path.lower().endswith('.tar.gz') or 
                       file_path.lower().endswith('.tar.lzo') or 
                       file_path.lower().endswith('.tar.zst') or
                       file_path.lower().endswith('.vma.gz') or 
                       file_path.lower().endswith('.vma.lzo') or 
                       file_path.lower().endswith('.vma.zst')):
                    continue

                # 获取文件修改时间
                propstat = response.find('.//{DAV:}propstat')
                if propstat is None:
                    continue

                prop = propstat.find('.//{DAV:}prop')
                if prop is None:
                    continue

                getlastmodified = prop.find('.//{DAV:}getlastmodified')
                if getlastmodified is None:
                    continue

                try:
                    # 解析时间字符串
                    from email.utils import parsedate_to_datetime
                    file_time = parsedate_to_datetime(getlastmodified.text).timestamp()
                    files.append({
                        'path': file_path,
                        'time': file_time
                    })
                except Exception as e:
                    logger.error(f"{self.plugin_name} 解析WebDAV文件时间失败: {e}")
                    # 如果无法解析时间，使用当前时间
                    files.append({
                        'path': file_path,
                        'time': time.time()
                    })

            # 按时间排序
            files.sort(key=lambda x: x['time'], reverse=True)

            # 删除超出保留数量的旧文件
            if len(files) > self._webdav_keep_backup_num:
                files_to_delete = files[self._webdav_keep_backup_num:]
                logger.info(f"{self.plugin_name} 找到 {len(files_to_delete)} 个WebDAV旧备份文件需要删除")

                for file_info in files_to_delete:
                    try:
                        # 从href中提取文件名
                        file_path = file_info['path']
                        if file_path.startswith('/'):
                            file_path = file_path[1:]
                        
                        # 构建删除URL
                        delete_url = urljoin(working_url + '/', file_path)
                        filename = os.path.basename(file_path)

                        # 删除文件
                        delete_response = requests.delete(
                            delete_url,
                            auth=(self._webdav_username, self._webdav_password),
                            headers={'User-Agent': 'MoviePilot/1.0'},
                            timeout=30,
                            verify=False
                        )

                        if delete_response.status_code in [200, 201, 204, 404]:  # 404意味着文件已经不存在
                            logger.info(f"{self.plugin_name} 成功删除WebDAV旧备份文件: {filename}")
                        else:
                            logger.error(f"{self.plugin_name} 删除文件失败: {filename}, 状态码: {delete_response.status_code}")

                    except Exception as e:
                        logger.error(f"{self.plugin_name} 处理WebDAV文件时发生错误: {str(e)}")

        except Exception as e:
            logger.error(f"{self.plugin_name} 清理WebDAV旧备份文件时发生错误: {str(e)}")

    def _clear_all_history(self):
        """清理所有历史记录"""
        try:
            self.save_data('backup_history', [])
            self.save_data('restore_history', [])
            logger.info(f"{self.plugin_name} 已清理所有历史记录")
            if self._notify:
                self._send_notification(
                    success=True,
                    message="已成功清理所有备份和恢复历史记录",
                    is_clear_history=True,
                    backup_details={}
                )
        except Exception as e:
            error_msg = f"清理历史记录失败: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            if self._notify:
                self._send_notification(
                    success=False,
                    message=error_msg,
                    is_clear_history=True,
                    backup_details={}
                )

    def _send_notification(self, success: bool, message: str = "", filename: Optional[str] = None, is_clear_history: bool = False, backup_details: Optional[Dict[str, Any]] = None):
        """发送通知（分隔线+emoji+结构化字段+结尾祝贺语，区分单/多容器）"""
        if not self._notify:
            return
        try:
            # 判断单容器还是多容器
            file_list = []
            if backup_details and "downloaded_files" in backup_details and backup_details["downloaded_files"]:
                file_list = [f["filename"] for f in backup_details["downloaded_files"]]
            is_multi = len(file_list) > 1
            
            # 标题
            status_emoji = "✅" if success else "❌"
            title_emoji = "🛠️"
            
            # 根据操作类型设置不同的标题
            if is_clear_history:
                title = f"{title_emoji} {self.plugin_name} 清理历史记录{'成功' if success else '失败'}"
            elif is_multi:
                title = f"{title_emoji} {self.plugin_name} 多容器备份{'成功' if success else '失败'}"
            else:
                title = f"{title_emoji} {self.plugin_name} 备份{'成功' if success else '失败'}"
            
            divider = "━━━━━━━━━━━━━━━━━━━━━━━━━"
            
            # 根据操作类型构建不同的通知内容
            if is_clear_history:
                # 清理历史记录专用格式
                status_str = f"{status_emoji} 清理历史记录{'成功' if success else '失败'}"
                host_str = self._pve_host or "-"
                detail_str = message.strip() if message else ("历史记录清理完成" if success else "历史记录清理失败")
                end_str = "✨ 历史记录清理完成！" if success else "❗ 历史记录清理失败，请检查日志！"
                
                text_content = (
                    f"{divider}\n"
                    f"📣 状态：{status_str}\n"
                    f"🔗 主机：{host_str}\n"
                    f"📋 详情：{detail_str}\n"
                    f"{divider}\n"
                    f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{end_str}"
                )
            else:
                # 备份操作格式
                status_str = f"{status_emoji} 备份{'成功' if success else '失败'}"
                host_str = self._pve_host or "-"
                if is_multi:
                    file_str = "\n".join(file_list)
                elif file_list:
                    file_str = file_list[0]
                else:
                    file_str = "-"
                path_str = "-"
                if backup_details and "downloaded_files" in backup_details and backup_details["downloaded_files"]:
                    details = backup_details["downloaded_files"][0]["details"]
                    if details["local_backup"]["enabled"] and details["local_backup"]["success"]:
                        path_str = details["local_backup"]["path"]
                # 详情
                if is_multi:
                    detail_str = f"共备份 {len(file_list)} 个容器。" + (message.strip() if message else ("备份已成功完成" if success else "备份失败，请检查日志"))
                else:
                    detail_str = message.strip() if message else ("备份已成功完成" if success else "备份失败，请检查日志")
                # 结尾祝贺语
                end_str = "✨ 备份已成功完成！" if success else "❗ 备份失败，请检查日志！"
                
                text_content = (
                    f"{divider}\n"
                    f"📣 状态：{status_str}\n"
                    f"🔗 主机：{host_str}\n"
                    f"📄 备份文件：{file_str}\n"
                    f"📁 路径：{path_str}\n"
                    f"📋 详情：{detail_str}\n"
                    f"{divider}\n"
                    f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{end_str}"
                )
            
            mtype = getattr(NotificationType, self._notification_message_type, NotificationType.Plugin)
            self.post_message(
                title=title,
                text=text_content,
                mtype=mtype
            )
        except Exception as e:
            logger.error(f"{self.plugin_name} 发送通知失败: {str(e)}")

    def _load_backup_history(self) -> List[Dict[str, Any]]:
        """加载备份历史记录"""
        history = self.get_data('backup_history')
        if history is None:
            return []
        if not isinstance(history, list):
            logger.error(f"{self.plugin_name} 历史记录数据格式不正确 (期望列表，得到 {type(history)})。将返回空历史。")
            return []
        return history

    def _save_backup_history_entry(self, entry: Dict[str, Any]):
        """保存单条备份历史记录"""
        try:
            # 加载现有历史记录
            history = self._load_backup_history()
            
            # 添加新记录到开头
            history.insert(0, entry)
            
            # 如果超过最大记录数，删除旧记录
            if len(history) > self._max_history_entries:
                history = history[:self._max_history_entries]
            
            # 保存更新后的历史记录
            self.save_data('backup_history', history)
            logger.debug(f"{self.plugin_name} 已保存备份历史记录")
        except Exception as e:
            logger.error(f"{self.plugin_name} 保存备份历史记录失败: {str(e)}")

    def _get_available_backups(self) -> List[Dict[str, Any]]:
        """获取可用的备份文件列表"""
        backups = []
        
        # 获取本地备份文件
        if self._enable_local_backup:
            try:
                # 如果_backup_path为空，使用默认路径
                backup_dir = Path(self._backup_path) if self._backup_path else Path(self.get_data_path()) / "actual_backups"
                if backup_dir.is_dir():
                    for file_path in backup_dir.iterdir():
                        if file_path.is_file() and (
                            file_path.name.endswith('.tar.gz') or 
                            file_path.name.endswith('.tar.lzo') or 
                            file_path.name.endswith('.tar.zst') or
                            file_path.name.endswith('.vma.gz') or 
                            file_path.name.endswith('.vma.lzo') or 
                            file_path.name.endswith('.vma.zst')
                        ):
                            try:
                                stat = file_path.stat()
                                size_mb = stat.st_size / (1024 * 1024)
                                time_str = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                                
                                backups.append({
                                    'filename': file_path.name,
                                    'path': str(file_path),
                                    'size_mb': size_mb,
                                    'time_str': time_str,
                                    'source': '本地备份'
                                })
                            except Exception as e:
                                logger.error(f"{self.plugin_name} 处理本地备份文件 {file_path.name} 时出错: {e}")
            except Exception as e:
                logger.error(f"{self.plugin_name} 获取本地备份文件列表失败: {e}")
        
        # 获取WebDAV备份文件
        if self._enable_webdav and self._webdav_url:
            try:
                webdav_backups = self._get_webdav_backups()
                backups.extend(webdav_backups)
            except Exception as e:
                logger.error(f"{self.plugin_name} 获取WebDAV备份文件列表失败: {e}")
        
        # 按时间排序（最新的在前）
        backups.sort(key=lambda x: datetime.strptime(x['time_str'], '%Y-%m-%d %H:%M:%S'), reverse=True)
        
        return backups

    def _get_webdav_backups(self) -> List[Dict[str, Any]]:
        """获取WebDAV上的备份文件列表"""
        backups = []
        
        try:
            import requests
            from urllib.parse import urljoin, urlparse
            from xml.etree import ElementTree
            
            # 构建WebDAV基础URL
            base_url = self._webdav_url.rstrip('/')
            webdav_path = self._webdav_path.lstrip('/')
            
            # 检测是否为Alist服务器（端口5244）
            parsed_url = urlparse(self._webdav_url)
            is_alist = parsed_url.port == 5244 or '5244' in self._webdav_url
            
            # 构建可能的URL列表
            possible_urls = []
            if is_alist:
                # Alist的特殊路径结构
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/dav/{webdav_path}",      # Alist标准路径
                        f"{base_url}/{webdav_path}"           # 直接路径
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}/dav",      # Alist标准路径
                        f"{base_url}"           # 直接路径
                    ])
            else:
                # 标准WebDAV路径
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/{webdav_path}",
                        f"{base_url}/dav/{webdav_path}",
                        f"{base_url}/remote.php/webdav/{webdav_path}",
                        f"{base_url}/dav/files/{self._webdav_username}/{webdav_path}"
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}",
                        f"{base_url}/dav",
                        f"{base_url}/remote.php/webdav"
                    ])
            
            # 尝试不同的URL结构
            working_url = None
            for test_url in possible_urls:
                try:
                    response = requests.request(
                        'PROPFIND',
                        test_url,
                        auth=(self._webdav_username, self._webdav_password),
                        headers={
                            'Depth': '1',
                            'Content-Type': 'application/xml',
                            'Accept': '*/*',
                            'User-Agent': 'MoviePilot/1.0'
                        },
                        timeout=10,
                        verify=False
                    )
                    if response.status_code == 207:
                        working_url = test_url
                        break
                except Exception as e:
                    logger.debug(f"{self.plugin_name} 测试WebDAV清理URL失败: {test_url}, 错误: {e}")
                    continue
            
            if not working_url:
                return backups
            
            # 发送PROPFIND请求获取文件列表
            response = requests.request(
                'PROPFIND',
                working_url,
                auth=(self._webdav_username, self._webdav_password),
                headers={
                    'Depth': '1',
                    'Content-Type': 'application/xml',
                    'Accept': '*/*',
                    'User-Agent': 'MoviePilot/1.0'
                },
                timeout=30,
                verify=False
            )

            if response.status_code != 207:
                return backups

            # 解析XML响应
            root = ElementTree.fromstring(response.content)
            
            for response_elem in root.findall('.//{DAV:}response'):
                href = response_elem.find('.//{DAV:}href')
                if href is None or not href.text:
                    continue

                file_path = href.text
                # 只处理Proxmox备份文件
                if not (file_path.lower().endswith('.tar.gz') or 
                       file_path.lower().endswith('.tar.lzo') or 
                       file_path.lower().endswith('.tar.zst') or
                       file_path.lower().endswith('.vma.gz') or 
                       file_path.lower().endswith('.vma.lzo') or 
                       file_path.lower().endswith('.vma.zst')):
                    continue

                # 获取文件信息
                propstat = response_elem.find('.//{DAV:}propstat')
                if propstat is None:
                    continue

                prop = propstat.find('.//{DAV:}prop')
                if prop is None:
                    continue

                # 获取文件大小
                getcontentlength = prop.find('.//{DAV:}getcontentlength')
                size_mb = 0
                if getcontentlength is not None and getcontentlength.text:
                    size_mb = int(getcontentlength.text) / (1024 * 1024)

                # 获取文件修改时间
                getlastmodified = prop.find('.//{DAV:}getlastmodified')
                time_str = "未知"
                if getlastmodified is not None and getlastmodified.text:
                    try:
                        from email.utils import parsedate_to_datetime
                        file_time = parsedate_to_datetime(getlastmodified.text)
                        time_str = file_time.strftime('%Y-%m-%d %H:%M:%S')
                    except Exception:
                        pass

                filename = os.path.basename(file_path)
                backups.append({
                    'filename': filename,
                    'path': file_path,
                    'size_mb': size_mb,
                    'time_str': time_str,
                    'source': 'WebDAV备份'
                })

        except Exception as e:
            logger.error(f"{self.plugin_name} 获取WebDAV备份文件列表时发生错误: {str(e)}")
        
        return backups

    def run_restore_job(self, filename: str, source: str = "本地备份"):
        """执行恢复任务"""
        if not self._enable_restore:
            logger.error(f"{self.plugin_name} 恢复功能未启用")
            return
        
        if not self._restore_lock:
            self._restore_lock = threading.Lock()
        if not self._global_task_lock:
            self._global_task_lock = threading.Lock()
            
        # 尝试获取全局任务锁，如果获取不到说明有其他任务在运行
        if not self._global_task_lock.acquire(blocking=False):
            logger.debug(f"{self.plugin_name} 检测到其他任务正在执行，恢复任务跳过！")
            return
            
        # 尝试获取恢复锁，如果获取不到说明有恢复任务在运行
        if not self._restore_lock.acquire(blocking=False):
            logger.debug(f"{self.plugin_name} 已有恢复任务正在执行，本次操作跳过！")
            self._global_task_lock.release()  # 释放全局锁
            return
            
        restore_entry = {
            "timestamp": time.time(),
            "success": False,
            "filename": filename,
            "target_vmid": self._restore_vmid or "自动",
            "message": "恢复任务开始"
        }
        self._restore_activity = "任务开始"
            
        try:
            logger.info(f"{self.plugin_name} 开始执行恢复任务，文件: {filename}, 来源: {source}")

            if not self._pve_host or not self._ssh_username or (not self._ssh_password and not self._ssh_key_file):
                error_msg = "配置不完整：PVE主机地址、SSH用户名或SSH认证信息(密码/密钥)未设置。"
                logger.error(f"{self.plugin_name} {error_msg}")
                self._send_restore_notification(success=False, message=error_msg, filename=filename)
                restore_entry["message"] = error_msg
                self._save_restore_history_entry(restore_entry)
                return

            # 执行恢复操作
            success, error_msg, target_vmid = self._perform_restore_once(filename, source)
            
            restore_entry["success"] = success
            restore_entry["target_vmid"] = target_vmid or self._restore_vmid or "自动"
            restore_entry["message"] = "恢复成功" if success else f"恢复失败: {error_msg}"
            
            self._send_restore_notification(success=success, message=restore_entry["message"], filename=filename, target_vmid=target_vmid)
                
        except Exception as e:
            logger.error(f"{self.plugin_name} 恢复任务执行主流程出错：{str(e)}")
            restore_entry["message"] = f"恢复任务执行主流程出错: {str(e)}"
            self._send_restore_notification(success=False, message=restore_entry["message"], filename=filename)
        finally:
            self._restore_activity = "空闲"
            self._save_restore_history_entry(restore_entry)
            # 确保锁一定会被释放
            if self._restore_lock and hasattr(self._restore_lock, 'locked') and self._restore_lock.locked():
                try:
                    self._restore_lock.release()
                except RuntimeError:
                    pass
            # 释放全局任务锁
            if self._global_task_lock and hasattr(self._global_task_lock, 'locked') and self._global_task_lock.locked():
                try:
                    self._global_task_lock.release()
                except RuntimeError:
                    pass
            logger.info(f"{self.plugin_name} 恢复任务执行完成。")

    def _perform_restore_once(self, filename: str, source: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        执行一次恢复操作
        :return: (是否成功, 错误消息, 目标VMID)
        """
        if not self._pve_host:
            return False, "未配置PVE主机地址", None

        # 创建SSH客户端
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sftp = None
        
        try:
            # 尝试SSH连接
            try:
                if self._ssh_key_file:
                    private_key = paramiko.RSAKey.from_private_key_file(self._ssh_key_file)
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, pkey=private_key)
                else:
                    ssh.connect(self._pve_host, port=self._ssh_port, username=self._ssh_username, password=self._ssh_password)
                logger.info(f"{self.plugin_name} SSH连接成功")
            except Exception as e:
                return False, f"SSH连接失败: {str(e)}", None

            # 1. 获取备份文件
            backup_file_path = None
            if source == "本地备份":
                backup_file_path = os.path.join(self._backup_path, filename)
                if not os.path.exists(backup_file_path):
                    return False, f"本地备份文件不存在: {backup_file_path}", None
            elif source == "WebDAV备份":
                # 从WebDAV下载备份文件到临时目录
                temp_dir = Path(self.get_data_path()) / "temp"
                temp_dir.mkdir(parents=True, exist_ok=True)
                backup_file_path = str(temp_dir / filename)
                
                self._restore_activity = f"下载WebDAV中: {filename}"
                download_success, download_error = self._download_from_webdav(filename, backup_file_path)
                if not download_success:
                    self._restore_activity = "空闲"
                    return False, f"从WebDAV下载备份文件失败: {download_error}", None
            else:
                return False, f"不支持的备份来源: {source}", None

            # 2. 上传备份文件到PVE
            sftp = ssh.open_sftp()
            remote_backup_path = f"/tmp/{filename}"
            
            self._restore_activity = f"上传PVE中: {filename}"
            logger.info(f"{self.plugin_name} 开始上传备份文件到PVE...")
            logger.info(f"{self.plugin_name} 本地路径: {backup_file_path}")
            logger.info(f"{self.plugin_name} 远程路径: {remote_backup_path}")
            
            # 获取文件大小
            local_stat = os.stat(backup_file_path)
            total_size = local_stat.st_size
            
            # 使用回调函数显示进度
            last_progress = -1  # 记录上次显示的进度
            def progress_callback(transferred: int, total: int):
                nonlocal last_progress
                if total > 0:
                    progress = (transferred / total) * 100
                    # 每20%显示一次进度
                    current_progress = int(progress / 20) * 20
                    if current_progress > last_progress or progress > 99.9:
                        self._restore_activity = f"上传PVE中: {progress:.1f}%"
                        logger.info(f"{self.plugin_name} 上传进度: {progress:.1f}%")
                        last_progress = current_progress
            
            # 上传文件
            sftp.put(backup_file_path, remote_backup_path, callback=progress_callback)
            logger.info(f"{self.plugin_name} 备份文件上传完成")

            # 3. 检查备份文件中的VMID
            original_vmid = self._extract_vmid_from_backup(filename)
            target_vmid = self._restore_vmid or original_vmid
            
            if not target_vmid:
                return False, "无法从备份文件名中提取VMID，请手动指定目标VMID", None

            # 4. 检查目标VM是否已存在
            vm_exists = self._check_vm_exists(ssh, target_vmid)
            if vm_exists:
                if self._restore_skip_existing:
                    return False, f"目标VM {target_vmid} 已存在，跳过恢复", target_vmid
                elif not self._restore_force:
                    return False, f"目标VM {target_vmid} 已存在，请启用强制恢复或跳过已存在选项", target_vmid
                else:
                    # 强制恢复：删除现有VM
                    logger.info(f"{self.plugin_name} 目标VM {target_vmid} 已存在，执行强制恢复")
                    delete_success, delete_error = self._delete_vm(ssh, target_vmid)
                    if not delete_success:
                        return False, f"删除现有VM失败: {delete_error}", target_vmid

            # 5. 执行恢复命令
            is_lxc = 'lxc' in filename.lower()
            if is_lxc:
                restore_cmd = f"pct restore {target_vmid} {remote_backup_path}"
            else:
                restore_cmd = f"qmrestore {remote_backup_path} {target_vmid}"

            if self._restore_storage:
                restore_cmd += f" --storage {self._restore_storage}"
            
            self._restore_activity = "等待PVE恢复中..."
            logger.info(f"{self.plugin_name} 执行恢复命令: {restore_cmd}")
            stdin, stdout, stderr = ssh.exec_command(restore_cmd)
    
            # 实时输出恢复日志
            while True:
                line = stdout.readline()
                if not line:
                    break
                logger.info(f"{self.plugin_name} 恢复输出: {line.strip()}")
            
            # 等待命令完成
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_output = stderr.read().decode().strip()
                return False, f"恢复失败: {error_output}", target_vmid

            logger.info(f"{self.plugin_name} 恢复成功完成，目标VMID: {target_vmid}")
            
            # 6. 清理临时文件
            try:
                sftp.remove(remote_backup_path)
                logger.info(f"{self.plugin_name} 已删除远程临时文件: {remote_backup_path}")
            except Exception as e:
                logger.warning(f"{self.plugin_name} 删除远程临时文件失败: {str(e)}")
            
            # 如果是WebDAV备份，删除本地临时文件
            if source == "WebDAV备份":
                try:
                    os.remove(backup_file_path)
                    logger.info(f"{self.plugin_name} 已删除本地临时文件: {backup_file_path}")
                except Exception as e:
                    logger.warning(f"{self.plugin_name} 删除本地临时文件失败: {str(e)}")
            
            return True, None, target_vmid

        except Exception as e:
            error_msg = f"恢复过程中发生错误: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg, None
            
        finally:
            # 确保关闭SFTP和SSH连接
            if sftp:
                try:
                    sftp.close()
                except:
                    pass
            if ssh:
                try:
                    ssh.close()
                except:
                    pass

    def _extract_vmid_from_backup(self, filename: str) -> Optional[str]:
        """从备份文件名中提取VMID"""
        try:
            # 备份文件名格式通常是: vzdump-{type}-{VMID}-{timestamp}.{format}.{compression}
            # 支持格式: tar.gz, tar.lzo, tar.zst, vma.gz, vma.lzo, vma.zst
            match = re.search(r'vzdump-(?:qemu|lxc)-(\d+)-', filename)
            if match:
                return match.group(1)
            return None
        except Exception as e:
            logger.error(f"{self.plugin_name} 从备份文件名提取VMID失败: {e}")
            return None

    def _check_vm_exists(self, ssh: paramiko.SSHClient, vmid: str) -> bool:
        """检查VM或CT是否存在"""
        try:
            # 检查QEMU VM
            check_qm_cmd = f"qm list | grep -q '^{vmid}\\s'"
            stdin, stdout, stderr = ssh.exec_command(check_qm_cmd)
            if stdout.channel.recv_exit_status() == 0:
                return True
            
            # 检查LXC容器
            check_pct_cmd = f"pct list | grep -q '^{vmid}\\s'"
            stdin, stdout, stderr = ssh.exec_command(check_pct_cmd)
            if stdout.channel.recv_exit_status() == 0:
                return True
                
            return False
        except Exception as e:
            logger.error(f"{self.plugin_name} 检查VM/CT存在性失败: {e}")
            return False

    def _delete_vm(self, ssh: paramiko.SSHClient, vmid: str, is_lxc: bool) -> Tuple[bool, Optional[str]]:
        """删除VM或CT"""
        try:
            cmd_prefix = "pct" if is_lxc else "qm"
            # 先停止VM/CT
            stop_cmd = f"{cmd_prefix} stop {vmid}"
            logger.info(f"{self.plugin_name} 尝试停止VM/CT: {stop_cmd}")
            stdin, stdout, stderr = ssh.exec_command(stop_cmd)
            stdout.channel.recv_exit_status()
            
            # 等待VM/CT完全停止
            time.sleep(5)
            
            # 删除VM/CT
            delete_cmd = f"{cmd_prefix} destroy {vmid}"
            logger.info(f"{self.plugin_name} 尝试删除VM/CT: {delete_cmd}")
            stdin, stdout, stderr = ssh.exec_command(delete_cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status != 0:
                error_output = stderr.read().decode().strip()
                if "does not exist" in error_output:
                    logger.warning(f"{self.plugin_name} 删除VM/CT {vmid} 时未找到，可能已被删除。")
                    return True, None
                return False, error_output
            
            logger.info(f"{self.plugin_name} 成功删除VM/CT {vmid}")
            return True, None
        except Exception as e:
            return False, str(e)

    def _download_from_webdav(self, filename: str, local_path: str) -> Tuple[bool, Optional[str]]:
        """从WebDAV下载备份文件"""
        try:
            import requests
            from urllib.parse import urljoin, urlparse
            
            # 构建WebDAV基础URL
            base_url = self._webdav_url.rstrip('/')
            webdav_path = self._webdav_path.lstrip('/')
            
            # 检测是否为Alist服务器（端口5244）
            parsed_url = urlparse(self._webdav_url)
            is_alist = parsed_url.port == 5244 or '5244' in self._webdav_url
            
            # 构建可能的下载URL列表
            possible_urls = []
            if is_alist:
                # Alist的特殊路径结构
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/dav/{webdav_path}/{filename}",      # Alist标准路径
                        f"{base_url}/{webdav_path}/{filename}"           # 直接路径
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}/dav/{filename}",      # Alist标准路径
                        f"{base_url}/{filename}"           # 直接路径
                    ])
            else:
                # 标准WebDAV路径
                if webdav_path:
                    possible_urls.extend([
                        f"{base_url}/{webdav_path}/{filename}",
                        f"{base_url}/dav/{webdav_path}/{filename}",
                        f"{base_url}/remote.php/webdav/{webdav_path}/{filename}",
                        f"{base_url}/dav/files/{self._webdav_username}/{webdav_path}/{filename}"
                    ])
                else:
                    possible_urls.extend([
                        f"{base_url}/{filename}",
                        f"{base_url}/dav/{filename}",
                        f"{base_url}/remote.php/webdav/{filename}"
                    ])
            
            # 尝试每个可能的URL
            for download_url in possible_urls:
                try:
                    logger.info(f"{self.plugin_name} 尝试从WebDAV下载文件: {download_url}")
                    
                    # 下载文件
                    response = requests.get(
                        download_url,
                        auth=(self._webdav_username, self._webdav_password),
                        headers={'User-Agent': 'MoviePilot/1.0'},
                        timeout=300,  # 5分钟超时
                        verify=False,
                        stream=True
                    )
                    
                    if response.status_code == 200:
                        # 获取文件大小
                        total_size = int(response.headers.get('content-length', 0))
                        downloaded_size = 0
                        last_progress = -1
                        
                        # 写入文件
                        with open(local_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    downloaded_size += len(chunk)
                                    if total_size > 0:
                                        progress = (downloaded_size / total_size) * 100
                                        current_progress = int(progress / 10) * 10
                                        if current_progress > last_progress:
                                            logger.info(f"{self.plugin_name} 下载进度: {progress:.1f}%")
                                            last_progress = current_progress
                        
                        logger.info(f"{self.plugin_name} 文件下载完成: {filename}")
                        return True, None
                    
                except Exception as e:
                    logger.warning(f"{self.plugin_name} 从URL下载失败: {download_url}, 错误: {str(e)}")
                    continue
            
            return False, "所有下载URL均失败"
            
        except Exception as e:
            error_msg = f"WebDAV下载过程中发生错误: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg

    def _load_restore_history(self) -> List[Dict[str, Any]]:
        """加载恢复历史记录"""
        history = self.get_data('restore_history')
        if history is None:
            return []
        if not isinstance(history, list):
            logger.error(f"{self.plugin_name} 恢复历史记录数据格式不正确 (期望列表，得到 {type(history)})。将返回空历史。")
            return []
        return history

    def _save_restore_history_entry(self, entry: Dict[str, Any]):
        """保存单条恢复历史记录"""
        try:
            # 加载现有历史记录
            history = self._load_restore_history()
            
            # 添加新记录到开头
            history.insert(0, entry)
            
            # 如果超过最大记录数，删除旧记录
            if len(history) > self._max_restore_history_entries:
                history = history[:self._max_restore_history_entries]
            
            # 保存更新后的历史记录
            self.save_data('restore_history', history)
            logger.debug(f"{self.plugin_name} 已保存恢复历史记录")
        except Exception as e:
            logger.error(f"{self.plugin_name} 保存恢复历史记录失败: {str(e)}")

    def _send_restore_notification(self, success: bool, message: str = "", filename: str = "", target_vmid: Optional[str] = None, is_clear_history: bool = False):
        """发送恢复通知"""
        if not self._notify: return
        
        title = f"🔄 {self.plugin_name} "
        if is_clear_history:
            title += "清理恢复历史记录"
        else:
            title += f"恢复{'成功' if success else '失败'}"
        status_emoji = "✅" if success else "❌"
        
        # 失败时的特殊处理
        if not success:
            divider_failure = "❌━━━━━━━━━━━━━━━━━━━━━━━━━❌"
            text_content = f"{divider_failure}\n"
        else:
            text_content = f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
        text_content += f"📣 状态：{status_emoji} 恢复{'成功' if success else '失败'}\n\n"
        text_content += f"🔗 路由：{self._pve_host}\n"
        
        if filename:
            text_content += f"📄 备份文件：{filename}\n"
        
        if target_vmid:
            text_content += f"🎯 目标VMID：{target_vmid}\n"
        
        if message:
            text_content += f"📋 详情：{message.strip()}\n"
        
        # 添加底部分隔线和时间戳
        if not success:
            text_content += f"\n{divider_failure}\n"
        else:
            text_content += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
        text_content += f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        # 根据成功/失败添加不同信息
        if success:
            text_content += "\n✨ 恢复已成功完成！"
        else:
            text_content += "\n❗ 恢复失败，请检查配置和连接！"
        
        try:
            mtype = getattr(NotificationType, self._notification_message_type, NotificationType.Plugin)
            self.post_message(mtype=mtype, title=title, text=text_content)
            logger.info(f"{self.plugin_name} 发送恢复通知: {title}")
        except Exception as e:
            logger.error(f"{self.plugin_name} 发送恢复通知失败: {e}")

    def _download_single_backup_file(self, ssh: paramiko.SSHClient, sftp: paramiko.SFTPClient, remote_file: str, backup_filename: str) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        """
        下载单个备份文件
        :return: (是否成功, 错误消息, 备份文件名, 备份详情)
        """
        try:
            # 确保文件路径是绝对路径
            if not remote_file.startswith('/'):
                remote_file = f"/var/lib/vz/dump/{remote_file}"
            
            # 验证文件是否存在
            check_cmd = f"test -f '{remote_file}' && echo 'exists'"
            stdin, stdout, stderr = ssh.exec_command(check_cmd)
            if stdout.read().decode().strip() != 'exists':
                return False, f"备份文件不存在: {remote_file}", None, {}
            
            # 下载文件
            local_path = os.path.join(self._backup_path, backup_filename)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # 获取文件大小
            remote_stat = sftp.stat(remote_file)
            total_size = remote_stat.st_size
            
            self._backup_activity = f"下载中: {backup_filename}"
            logger.info(f"{self.plugin_name} 开始下载备份文件: {backup_filename}")
            logger.info(f"{self.plugin_name} 远程路径: {remote_file}")
            logger.info(f"{self.plugin_name} 本地路径: {local_path}")
            logger.info(f"{self.plugin_name} 文件大小: {total_size / 1024 / 1024:.2f} MB")
            
            # 使用回调函数显示进度
            last_progress = -1  # 记录上次显示的进度
            def progress_callback(transferred: int, total: int):
                nonlocal last_progress
                if total > 0:
                    progress = (transferred / total) * 100
                    # 每20%显示一次进度
                    current_progress = int(progress / 20) * 20
                    if current_progress > last_progress or progress > 99.9:
                        self._backup_activity = f"下载中: {progress:.1f}%"
                        logger.info(f"{self.plugin_name} 下载进度: {progress:.1f}%")
                        last_progress = current_progress
                    elif progress > 99.89 and progress < 99.91 and last_progress < 99:  # 只显示一次99.9%
                        self._backup_activity = f"下载中: 99.9%"
                        logger.info(f"{self.plugin_name} 下载进度: 99.9%")
                        last_progress = 99
                    elif progress >= 100 and last_progress < 100:  # 只在最后显示一次100%
                        self._backup_activity = f"下载中: 100.0%"
                        logger.info(f"{self.plugin_name} 下载进度: 100.0%")
                        last_progress = 100
            
            # 下载文件
            sftp.get(remote_file, local_path, callback=progress_callback)
            logger.info(f"{self.plugin_name} 文件下载完成: {backup_filename}")
            
            # 如果配置了下载后删除
            if self._auto_delete_after_download:
                try:
                    sftp.remove(remote_file)
                    logger.info(f"{self.plugin_name} 已删除远程备份文件: {remote_file}")
                except Exception as e:
                    logger.error(f"{self.plugin_name} 删除远程备份文件失败: {str(e)}")

            # 构建备份详情
            backup_details = {
                "local_backup": {
                    "enabled": self._enable_local_backup,
                    "success": True,
                    "path": self._backup_path,
                    "filename": backup_filename
                },
                "webdav_backup": {
                    "enabled": self._enable_webdav and bool(self._webdav_url),
                    "success": False,
                    "url": self._webdav_url,
                    "path": self._webdav_path,
                    "filename": backup_filename,
                    "error": None
                }
            }

            # 如果启用了WebDAV备份,上传到WebDAV
            if self._enable_webdav and self._webdav_url:
                self._backup_activity = f"上传WebDAV中: {backup_filename}"
                webdav_success, webdav_error = self._upload_to_webdav(local_path, backup_filename)
                backup_details["webdav_backup"]["success"] = webdav_success
                backup_details["webdav_backup"]["error"] = webdav_error
                
                if webdav_success:
                    logger.info(f"{self.plugin_name} WebDAV备份成功: {backup_filename}")
                else:
                    logger.error(f"{self.plugin_name} WebDAV备份失败: {backup_filename} - {webdav_error}")
            
            return True, None, backup_filename, backup_details
            
        except Exception as e:
            error_msg = f"下载备份文件 {backup_filename} 时发生错误: {str(e)}"
            logger.error(f"{self.plugin_name} {error_msg}")
            return False, error_msg, None, {}

# ===== 模块级API函数 =====
def api_restore_backup(filename: str, source: str = "本地备份"):
    plugin = ProxmoxVEBackup.get_instance()
    if plugin is None:
        return {"success": False, "message": "插件实例未初始化"}
    try:
        plugin.run_restore_job(filename, source)
        return {"success": True, "message": "恢复任务已启动"}
    except Exception as e:
        return {"success": False, "message": f"启动恢复任务失败: {str(e)}"}