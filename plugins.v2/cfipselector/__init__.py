import time
import random
import requests
import ipaddress
import subprocess
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from app.plugins import _PluginBase
from app.log import logger
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.schemas import NotificationType
from typing import Any, List, Dict, Tuple, Optional
import shutil
import urllib.request
import zipfile, tarfile
import json
from collections import defaultdict

class CFIPSelector(_PluginBase):
    plugin_name = "PT云盾优选"
    plugin_desc = "PT站点专属优选IP，自动写入hosts，访问快人一步"
    plugin_icon = "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins/refs/heads/main/icons/cfipselector.png"
    plugin_version = "1.0.0"
    plugin_author = "M.Jinxi"
    author_url = "https://github.com/xijin285"
    plugin_config_prefix = "cfipselector_"
    plugin_order = 2
    auth_level = 2

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _cron: str = "0 3 * * *"
    _onlyonce: bool = False
    _notify: bool = False
    _datacenters: str = "HKG,SJC"
    _delay: int = 1500
    _ip_type: str = "4"
    _port: int = 443
    _tls: bool = True
    _ipnum: int = 10
    _sign_sites: List[str] = []
    sites: Optional[object] = None
    siteoper: Optional[object] = None
    _last_select_time = ''
    _last_selected_ip = ''

    def init_plugin(self, config: dict = None):
        logger.info("CFIPSelector插件已加载")
        self.stop_service()  # 每次都先彻底停止服务
        self.sites = None
        self.siteoper = None
        self._sign_sites = []
        self._last_select_time = ''
        self._last_selected_ip = ''
        try:
            from app.helper.sites import SitesHelper
            from app.db.site_oper import SiteOper
            self.sites = SitesHelper()
            self.siteoper = SiteOper()
        except Exception as e:
            logger.warning(f"未能加载站点数据源: {e}")
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._cron = str(config.get("cron", "0 3 * * *"))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", False))
            self._datacenters = str(config.get("datacenters", "HKG,SJC"))
            self._delay = int(config.get("delay", 1500))
            self._ip_type = str(config.get("ip_type", "4"))
            self._port = int(config.get("port", 443))
            self._tls = bool(config.get("tls", True))
            self._ipnum = int(config.get("ipnum", 10))
            raw_sign_sites = config.get("sign_sites") or []
            self._sign_sites = [str(i) for i in raw_sign_sites]
            self._last_select_time = config.get("last_select_time", "")
            self._last_selected_ip = config.get("last_selected_ip", "")
            all_ids = []
            if self.siteoper:
                try:
                    all_ids += [str(site.id) for site in self.siteoper.list_active()]
                except Exception:
                    pass
            try:
                custom_sites_config = self.get_config("CustomSites")
                if custom_sites_config and custom_sites_config.get("enabled"):
                    custom_sites = custom_sites_config.get("sites")
                    all_ids += [str(site.get("id")) for site in custom_sites]
            except Exception:
                pass
            self._sign_sites = [i for i in self._sign_sites if i in all_ids]
            self.__update_config()
        if self._enabled:
            if self._onlyonce:
                try:
                    if not self._scheduler or not self._scheduler.running:
                        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    job_name = f"{self.plugin_name}服务_onlyonce"
                    if self._scheduler.get_job(job_name):
                        self._scheduler.remove_job(job_name)
                    logger.info(f"{self.plugin_name} 服务启动，立即运行一次")
                    self._scheduler.add_job(func=self.select_ips, trigger='date',
                        run_date=datetime.now(), name=job_name, id=job_name)
                    self._onlyonce = False
                    self.__update_config()
                    if self._scheduler and not self._scheduler.running:
                        self._scheduler.start()
                except Exception as e:
                    logger.error(f"启动一次性 {self.plugin_name} 任务失败: {str(e)}")
            else:
                self.__add_task()
        else:
            logger.info("插件未启用")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "datacenters": self._datacenters,
            "delay": self._delay,
            "ip_type": self._ip_type,
            "port": self._port,
            "tls": self._tls,
            "ipnum": self._ipnum,
            "sign_sites": self._sign_sites or [],
            "last_select_time": getattr(self, '_last_select_time', ''),
            "last_selected_ip": getattr(self, '_last_selected_ip', ''),
        })

    def __add_task(self):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown()
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        self._scheduler.add_job(self.select_ips, 'cron', **self._parse_cron(self._cron))
        self._scheduler.start()
        logger.info(f"{self.plugin_name} 定时任务已启动")

    def _parse_cron(self, cron_str):
        parts = cron_str.split()
        return {'minute': int(parts[0]), 'hour': int(parts[1])}

    def _download_cf_ip_list(self, ip_type: int = 4) -> list:
        """
        优先读取resources/cfv4.txt/cfv6.txt，若不存在则自动下载Cloudflare官方IP段
        """
        local_file = os.path.join(os.path.dirname(__file__), 'resources', f"cfv{ip_type}.txt")
        if os.path.exists(local_file):
            try:
                with open(local_file, 'r', encoding='utf-8') as f:
                    lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                logger.info(f"读取本地resources/cfv{ip_type}.txt成功，共{len(lines)}条")
                return lines
            except Exception as e:
                logger.error(f"读取本地resources/cfv{ip_type}.txt失败: {e}")
        # 本地不存在则拉取官方
        url = "https://www.cloudflare.com/ips-v4" if ip_type == 4 else "https://www.cloudflare.com/ips-v6"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                lines = [line.strip() for line in resp.text.splitlines() if line.strip() and not line.startswith('#')]
                logger.info(f"获取Cloudflare官方IPv{ip_type}网段成功，共{len(lines)}条")
                return lines
            else:
                logger.error(f"获取Cloudflare官方IPv{ip_type}网段失败，状态码: {resp.status_code}")
        except Exception as e:
            logger.error(f"下载Cloudflare官方IPv{ip_type}网段异常: {e}")
        return []

    def _get_ip_pool(self, ip_type: int = 4, max_per_net: int = 10) -> list:
        """
        获取IP池：自动下载官方IP段并解析，返回部分真实IP
        """
        nets = self._download_cf_ip_list(ip_type)
        ip_pool = []
        for net in nets:
            try:
                net_obj = ipaddress.ip_network(net, strict=False)
                # 只取每个网段前max_per_net个IP，避免爆炸
                for idx, ip in enumerate(net_obj.hosts()):
                    if idx >= max_per_net:
                        break
                    ip_pool.append(str(ip))
            except Exception as e:
                logger.warning(f"解析网段{net}失败: {e}")
        logger.info(f"生成IPv{ip_type} IP池，共{len(ip_pool)}个IP")
        return ip_pool

    def _get_ip_pool_by_datacenters(self, ip_type: int, datacenters: List[str], max_per_net: int = 10) -> list:
        """
        只生成目标数据中心的IP池
        """
        loc_path = os.path.join(os.path.dirname(__file__), 'resources', 'locations.json')
        if os.path.exists(loc_path):
            with open(loc_path, 'r', encoding='utf-8') as f:
                locations = json.load(f)
        else:
            logger.warning("未找到本地 resources/locations.json，请在resources目录下自行维护数据中心映射表！")
            return []
        nets = []
        for dc in datacenters:
            info = locations.get(dc)
            if info:
                nets += [net for net in info.get('nets', []) if (':' in net if ip_type==6 else ':' not in net)]
        ip_pool = []
        for net in nets:
            try:
                net_obj = ipaddress.ip_network(net, strict=False)
                for idx, ip in enumerate(net_obj.hosts()):
                    if idx >= max_per_net:
                        break
                    ip_pool.append(str(ip))
            except Exception as e:
                logger.warning(f"解析网段{net}失败: {e}")
        logger.info(f"生成IPv{ip_type} IP池（仅目标数据中心），共{len(ip_pool)}个IP")
        return ip_pool

    def _download_locations_json(self):
        """
        优先读取本地resources/locations.json，找不到时再查找绝对路径兜底
        """
        loc_path = os.path.join(os.path.dirname(__file__), 'resources', 'locations.json')
        if os.path.exists(loc_path):
            with open(loc_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        logger.warning("未找到本地 resources/locations.json，请在resources目录下自行维护数据中心映射表！")
        return {}

    def _ip_to_datacenter(self, ip, locations):
        """
        根据locations.json映射IP到数据中心三字码
        """
        for colo, info in locations.items():
            for net in info.get('nets', []):
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(net, strict=False):
                        return colo
                except Exception:
                    continue
        return '?'

    def _tcp_ping(self, ip, port=443, timeout=1):
        """
        用socket connect检测IP延迟，失败返回9999
        """
        import socket
        try:
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(timeout)
            start = time.time()
            s.connect((ip, port))
            delay = (time.time() - start) * 1000
            s.close()
            return delay
        except Exception:
            return 9999

    def _get_selected_sites_info(self) -> List[Dict[str, Any]]:
        """
        获取选中站点的详细信息（id, name, domain）
        """
        infos = []
        if not self._sign_sites or not self.siteoper:
            return infos
        try:
            # 内置站点
            for site in self.siteoper.list_active():
                if str(site.id) in self._sign_sites:
                    infos.append({"id": str(site.id), "name": getattr(site, "name", str(site.id)), "domain": site.domain})
            # 自定义站点
            try:
                custom_sites_config = self.get_config("CustomSites")
                if custom_sites_config and custom_sites_config.get("enabled"):
                    custom_sites = custom_sites_config.get("sites")
                    for site in custom_sites:
                        if str(site.get("id")) in self._sign_sites:
                            infos.append({"id": str(site.get("id")), "name": site.get("name", str(site.get("id"))), "domain": site.get("domain")})
            except Exception as e:
                logger.warning(f"获取自定义站点失败: {e}")
        except Exception as e:
            logger.error(f"获取选中站点信息失败: {e}")
        return infos

    def _get_selected_sites_domains(self) -> List[str]:
        """
        获取选中站点的域名列表
        """
        if not self._sign_sites or not self.siteoper:
            return []
        
        domains = []
        try:
            # 获取内置站点
            for site in self.siteoper.list_active():
                if str(site.id) in self._sign_sites:
                    domains.append(site.domain)
            
            # 获取自定义站点
            try:
                custom_sites_config = self.get_config("CustomSites")
                if custom_sites_config and custom_sites_config.get("enabled"):
                    custom_sites = custom_sites_config.get("sites")
                    for site in custom_sites:
                        if str(site.get("id")) in self._sign_sites:
                            domains.append(site.get("domain"))
            except Exception as e:
                logger.warning(f"获取自定义站点失败: {e}")
                
        except Exception as e:
            logger.error(f"获取选中站点域名失败: {e}")
        
        logger.info(f"选中的检测站点域名: {domains}")
        return domains

    def _test_ip_with_sites(self, ip: str, domains: List[str], timeout: int = 5) -> Dict[str, Any]:
        """
        用临时hosts测试IP对站点的访问速度
        返回: {"total_delay": 总延迟, "success_count": 成功数, "total_count": 总数, "avg_delay": 平均延迟}
        """
        if not domains:
            return {"total_delay": 9999, "success_count": 0, "total_count": 0, "avg_delay": 9999}
        
        total_delay = 0
        success_count = 0
        total_count = len(domains)
        
        # 临时修改hosts进行测试
        original_hosts = self._read_system_hosts()
        try:
            # 添加临时hosts条目
            temp_hosts = []
            for domain in domains:
                temp_hosts.append(f"{ip} {domain}")
            
            self._add_temp_hosts(temp_hosts)
            
            # 测试每个站点
            for domain in domains:
                try:
                    start_time = time.time()
                    if self._tls:
                        url = f"https://{domain}"
                    else:
                        url = f"http://{domain}"
                    
                    # 添加重试机制
                    max_retries = 2
                    for retry in range(max_retries):
                        try:
                            response = requests.get(url, timeout=timeout, verify=False)
                            if response.status_code == 200:
                                delay = (time.time() - start_time) * 1000
                                total_delay += delay
                                success_count += 1
                                logger.debug(f"IP {ip} 访问 {domain} 成功，延迟: {delay:.2f}ms")
                                break
                            else:
                                logger.debug(f"IP {ip} 访问 {domain} 失败，状态码: {response.status_code}")
                                if retry == max_retries - 1:
                                    logger.debug(f"IP {ip} 访问 {domain} 重试{max_retries}次后仍失败")
                        except requests.exceptions.ConnectionError as e:
                            if "Connection reset by peer" in str(e):
                                logger.debug(f"IP {ip} 访问 {domain} 连接被重置 (重试 {retry+1}/{max_retries})")
                                if retry < max_retries - 1:
                                    time.sleep(0.5)  # 短暂等待后重试
                                    continue
                            else:
                                logger.debug(f"IP {ip} 访问 {domain} 连接异常: {e}")
                        except Exception as e:
                            logger.debug(f"IP {ip} 访问 {domain} 异常: {e}")
                            break
                            
                except Exception as e:
                    logger.debug(f"IP {ip} 访问 {domain} 异常: {e}")
            
        finally:
            # 恢复原始hosts
            self._restore_hosts(original_hosts)
        
        avg_delay = total_delay / success_count if success_count > 0 else 9999
        return {
            "total_delay": total_delay,
            "success_count": success_count,
            "total_count": total_count,
            "avg_delay": avg_delay
        }

    def _read_system_hosts(self) -> str:
        """
        读取系统hosts文件内容
        """
        try:
            import platform
            if platform.system() == "Windows":
                hosts_path = r"c:\windows\system32\drivers\etc\hosts"
            else:
                hosts_path = '/etc/hosts'
            
            with open(hosts_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取系统hosts失败: {e}")
            return ""

    def _add_temp_hosts(self, hosts_lines: List[str]):
        """
        添加临时hosts条目
        """
        try:
            import platform
            if platform.system() == "Windows":
                hosts_path = r"c:\windows\system32\drivers\etc\hosts"
            else:
                hosts_path = '/etc/hosts'
            
            with open(hosts_path, 'a', encoding='utf-8') as f:
                f.write("\n# CFIPSelector临时测试\n")
                for line in hosts_lines:
                    f.write(f"{line}\n")
        except Exception as e:
            logger.error(f"添加临时hosts失败: {e}")

    def _restore_hosts(self, original_content: str):
        """
        恢复原始hosts内容
        """
        try:
            import platform
            if platform.system() == "Windows":
                hosts_path = r"c:\windows\system32\drivers\etc\hosts"
            else:
                hosts_path = '/etc/hosts'
            
            # 移除临时条目
            lines = original_content.split('\n')
            filtered_lines = []
            skip_temp = False
            
            for line in lines:
                if "# CFIPSelector临时测试" in line:
                    skip_temp = True
                    continue
                if skip_temp and line.strip() == "":
                    skip_temp = False
                    continue
                if skip_temp:
                    continue
                filtered_lines.append(line)
            
            with open(hosts_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(filtered_lines))
        except Exception as e:
            logger.error(f"恢复hosts失败: {e}")

    def _write_hosts_for_sites(self, ip: str, domains: List[str]):
        """
        将选中站点的域名写入hosts，指向优选IP
        """
        if not domains:
            logger.warning("没有检测站点，跳过hosts写入")
            return False
        
        try:
            from python_hosts import Hosts, HostsEntry
            import platform
            
            if platform.system() == "Windows":
                hosts_path = r"c:\windows\system32\drivers\etc\hosts"
            else:
                hosts_path = '/etc/hosts'
            
            # 读取系统hosts
            system_hosts = Hosts(path=hosts_path)
            
            original_entries = []
            for entry in system_hosts.entries:
                if entry.entry_type == "comment" and entry.comment == "# CFIPSelector优选IP":
                    break
                original_entries.append(entry)
            system_hosts.entries = original_entries
            
            # 添加新的hosts条目
            new_entries = []
            new_entries.append(HostsEntry(entry_type='comment', comment="# CFIPSelector优选IP"))
            
            for domain in domains:
                try:
                    host_entry = HostsEntry(
                        entry_type='ipv6' if ':' in ip else 'ipv4',
                        address=ip,
                        names=[domain]
                    )
                    new_entries.append(host_entry)
                except Exception as e:
                    logger.error(f"创建hosts条目失败 {ip} {domain}: {e}")
            
            # 写入系统hosts
            system_hosts.add(new_entries)
            system_hosts.write()
            
            logger.info(f"成功写入hosts: {ip} -> {domains}")
            return True
            
        except Exception as e:
            logger.error(f"写入hosts失败: {e}")
            return False

    @eventmanager.register(EventType.PluginAction)
    def select_ips(self, event: Event = None):
        try:
            logger.info("开始优选IP...")
            test_sites_info = self._get_selected_sites_info()
            test_domains = [info["domain"] for info in test_sites_info]
            use_site_testing = len(test_domains) > 0
            if not use_site_testing:
                logger.warning("未选择检测站点，无法进行优选。")
                return
            ip_types = []
            ip_type_str = str(getattr(self, '_ip_type', '4'))
            if '4' in ip_type_str:
                ip_types.append(4)
            if '6' in ip_type_str:
                ip_types.append(6)
            if not ip_types:
                logger.warning("IPv4/IPv6均未启用，不进行优选。")
                return
            locations = self._download_locations_json()
            for ip_type in ip_types:
                ip_pool = self._get_ip_pool_by_datacenters(ip_type, [d.strip().upper() for d in self._datacenters.split(",") if d.strip()], max_per_net=10)
                for ip in ip_pool:
                    site_test_result = self._test_ip_with_sites(ip, test_domains, timeout=5)
                    if site_test_result["success_count"] > 0:
                        logger.info(f"优选成功，已写入hosts: {ip}")
                        hosts_status = self._write_hosts_for_sites(ip, test_domains)
                        if hosts_status:
                            from datetime import datetime
                            self._last_select_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            self._last_selected_ip = ip
                            self.__update_config()
                        else:
                            logger.warning(f"优选成功但写入hosts失败: {ip}")
                        if self._notify:
                            test_method = "HTTPS" if self._tls else "HTTP"
                            self._send_notification(True, f"优选完成，已找到可用IP: {ip}", [{"ip": ip, "test_method": test_method}], hosts_status=hosts_status)
                        return
            logger.warning("没有找到任何可用的优选IP！")
            if self._notify:
                self._send_notification(False, "优选失败，没有找到可用IP。", None, hosts_status=None)
        except Exception as e:
            logger.error(f"select_ips主流程异常: {e}")

    def _send_notification(self, success: bool, message: str = "", result: Optional[List[Dict[str, Any]]] = None, hosts_status: Optional[bool] = None):
        if not self._notify:
            return
        
        if success:
            title = "🛡️ PT云盾优选 - 优选成功"
            text = "✅ 优选任务执行成功\n"
            test_domains = self._get_selected_sites_domains()
            if test_domains and result:
                best_ip = result[0]["ip"]
                test_method = result[0].get("test_method", "HTTP")
                text += f"🌐 优选IP: {best_ip}\n"
                text += f"🔍 测试方式: {test_method}\n"
                text += f"📋 检测站点: {', '.join(test_domains)}\n"
            if message:
                text += f"📝 {message}\n"
            # hosts写入状态放在📝后面
            if hosts_status is not None:
                text += f"🖥️ hosts写入: {'成功' if hosts_status else '失败'}\n"
            text += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            title = "🛡️ PT云盾优选 - 优选失败"
            text = "❌ 优选任务执行失败\n"
            if message:
                text += f"📝 失败原因: {message}\n"
            if hosts_status is not None:
                text += f"🖥️ hosts写入: {'成功' if hosts_status else '失败'}\n"
            text += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "sync_locations",
                "label": "立即同步数据",
                "desc": "同步数据中心映射表",
                "icon": "mdi-sync"
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        logger.info(f"on_plugin_action收到事件: {event.data}")
        command = event.data.get("command")
        logger.info(f"收到插件动作命令: {command}")
        
        if command == "sync_locations":
            logger.info("收到插件动作：sync_locations，开始同步数据中心映射表...")
            result = self.api_sync_locations()
            logger.info(f"同步结果: {result}")
            return result
        else:
            logger.warning(f"未知的插件动作命令: {command}")
            return {"success": False, "msg": f"未知命令: {command}"}

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/cfipselector/select",
                "endpoint": self.api_select_now,
                "methods": ["POST"],
                "summary": "立即优选IP",
                "description": "手动触发一次Cloudflare IP优选"
            }
        ]

    def api_select_now(self, *args, **kwargs):
        self.select_ips()
        return {"msg": "已手动触发优选"}

    def api_sync_locations(self, *args, **kwargs):
        logger.info("同步数据中心API被调用")
        """
        同步数据中心映射表：直接处理resources/locations_raw.json
        """
        import json
        plugin_dir = os.path.dirname(__file__)
        raw_path = os.path.join(plugin_dir, 'resources', 'locations_raw.json')
        out_path = os.path.join(plugin_dir, 'resources', 'locations.json')
        
        logger.info(f"插件目录: {plugin_dir}")
        logger.info(f"原始数据文件: {raw_path}")
        logger.info(f"输出文件: {out_path}")
        
        if not os.path.exists(raw_path):
            logger.error(f"未找到原始数据文件: {raw_path}")
            return {"success": False, "msg": "未找到resources/locations_raw.json，请先上传原始数据！"}
        
        try:
            # 读取原始数据
            logger.info(f"开始读取原始数据文件: {raw_path}")
            with open(raw_path, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
            logger.info(f"原始数据读取成功，数据类型: {type(raw_data)}")
            
            # 处理数据格式（根据实际格式调整）
            processed_data = {}
            if isinstance(raw_data, dict):
                processed_data = raw_data
                logger.info("原始数据已经是标准格式，直接使用")
            elif isinstance(raw_data, list):
                for item in raw_data:
                    if isinstance(item, dict) and 'code' in item and 'nets' in item:
                        processed_data[item['code']] = {
                            'name': item.get('name', item['code']),
                            'nets': item['nets']
                        }
                logger.info(f"将列表格式转换为字典格式，共{len(processed_data)}个数据中心")
            else:
                logger.error(f"未知的数据格式: {type(raw_data)}")
                return {"success": False, "msg": f"未知的数据格式: {type(raw_data)}"}
            
            logger.info(f"开始写入处理后的数据到: {out_path}")
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(processed_data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"同步成功！共处理{len(processed_data)}个数据中心")
            return {"success": True, "msg": f"同步成功！共处理{len(processed_data)}个数据中心"}
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            return {"success": False, "msg": f"JSON解析失败：{e}"}
        except Exception as e:
            logger.error(f"同步异常: {e}")
            return {"success": False, "msg": f"同步异常：{e}"}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_options = []
        try:
            from app.db.site_oper import SiteOper
            siteoper = SiteOper()
            custom_sites = []
            try:
                custom_sites_config = self.get_config("CustomSites")
                if custom_sites_config and custom_sites_config.get("enabled"):
                    custom_sites = custom_sites_config.get("sites")
            except Exception:
                pass
            site_options = ([{"title": site.name, "value": str(site.id)} for site in siteoper.list_active()] +
                            [{"title": site.get("name"), "value": str(site.get("id"))} for site in custom_sites])
        except Exception as e:
            logger.warning(f"获取站点选项失败: {e}")
        form = [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件', 'color': 'primary', 'prepend-icon': 'mdi-power'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知', 'color': 'info', 'prepend-icon': 'mdi-bell'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'tls', 'label': '加密连接', 'color': 'primary', 'prepend-icon': 'mdi-lock'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行', 'color': 'success', 'prepend-icon': 'mdi-play'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                {'component': 'VSelect', 'props': {
                                    'chips': True,
                                    'closable-chips': True,
                                    'multiple': True,
                                    'model': 'sign_sites',
                                    'label': '检测站点',
                                    'items': site_options,
                                    'item-title': 'title',
                                    'item-value': 'value'
                                }}
                            ]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'port', 'label': '端口', 'placeholder': '443', 'prepend-inner-icon': 'mdi-lan'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'ip_type', 'label': 'IP类型(4/6/46)', 'placeholder': '4', 'prepend-inner-icon': 'mdi-numeric'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'datacenters', 'label': '数据中心(逗号分隔)', 'placeholder': 'HKG,SJC', 'prepend-inner-icon': 'mdi-database-search'}}]},
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'ipnum', 'label': '优选数量', 'placeholder': '10', 'prepend-inner-icon': 'mdi-counter'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'cron', 'label': '定时任务(cron)', 'placeholder': '0 3 * * *', 'prepend-inner-icon': 'mdi-clock-outline'}}]},
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'delay', 'label': '延迟阈值(ms)', 'placeholder': '1500', 'prepend-inner-icon': 'mdi-timer'}}]},
                        ]
                    },
                ]
            }
        ]
        model = {
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "datacenters": self._datacenters,
            "delay": self._delay,
            "ip_type": self._ip_type,
            "port": self._port,
            "tls": self._tls,
            "ipnum": self._ipnum,
            "sign_sites": self._sign_sites or [],
            "last_select_time": self._last_select_time,
            "last_selected_ip": self._last_selected_ip,
        }
        return form, model

    def get_page(self) -> List[dict]:
        import random
        enabled = self._enabled
        datacenters = self._datacenters
        last_select_time = getattr(self, '_last_select_time', '暂无记录')
        last_ip = getattr(self, '_last_selected_ip', '暂无')
        sign_sites = self._sign_sites or []
        site_names = []
        if hasattr(self, 'siteoper') and self.siteoper:
            try:
                for site in self.siteoper.list_active():
                    if str(site.id) in sign_sites:
                        site_names.append(getattr(site, 'name', str(site.id)))
            except Exception:
                pass
        cards = [
            {'component': 'VCardTitle', 'props': {'class': 'text-h6 font-weight-bold', 'style': 'display: flex; align-items: center;'},
             'content': [
                 {'component': 'span', 'text': '当前状态'}
             ]},
            {'component': 'VDivider', 'props': {'class': 'mb-2'}},
            {'component': 'VRow', 'content': [
                {'component': 'VCol', 'props': {'cols': 4, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VIcon', 'props': {'icon': 'mdi-power', 'color': 'success' if enabled else 'grey', 'class': 'mr-1'}},
                    {'component': 'span', 'text': '插件状态'}
                ]},
                {'component': 'VCol', 'props': {'cols': 8, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VChip', 'props': {
                        'color': 'success' if enabled else 'grey',
                        'label': True
                    }, 'text': '已启用' if enabled else '已禁用'}
                ]}
            ]},
            {'component': 'VRow', 'content': [
                {'component': 'VCol', 'props': {'cols': 4, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VIcon', 'props': {'icon': 'mdi-database-search', 'color': 'info', 'class': 'mr-1'}},
                    {'component': 'span', 'text': '目标数据中心'}
                ]},
                {'component': 'VCol', 'props': {'cols': 8, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VChip', 'props': {'color': 'info', 'label': True}, 'text': datacenters or '未设置'}
                ]}
            ]},
            {'component': 'VRow', 'content': [
                {'component': 'VCol', 'props': {'cols': 4, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VIcon', 'props': {'icon': 'mdi-clock-outline', 'color': 'primary', 'class': 'mr-1'}},
                    {'component': 'span', 'text': '上次优选时间'}
                ]},
                {'component': 'VCol', 'props': {'cols': 8, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VChip', 'props': {'color': 'primary', 'label': True}, 'text': last_select_time}
                ]}
            ]},
            {'component': 'VRow', 'content': [
                {'component': 'VCol', 'props': {'cols': 4, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VIcon', 'props': {'icon': 'mdi-lan', 'color': 'primary', 'class': 'mr-1'}},
                    {'component': 'span', 'text': '当前 hosts IP'}
                ]},
                {'component': 'VCol', 'props': {'cols': 8, 'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VChip', 'props': {'color': 'primary', 'label': True}, 'text': last_ip}
                ]}
            ]},
        ]
        chips = []
        color_choices = ['info', 'success', 'primary', 'warning', 'error', 'secondary']
        for name in site_names:
            chips.append({
                'component': 'VChip',
                'props': {
                    'color': random.choice(color_choices),
                    'label': True,
                    'class': 'ma-1'
                },
                'text': name
            })
        cards.append({
            'component': 'VCardTitle', 'props': {'class': 'text-h6 font-weight-bold', 'style': 'display: flex; align-items: center;'},
            'content': [
                {'component': 'VIcon', 'props': {'icon': 'mdi-domain', 'color': 'info', 'size': 24, 'class': 'mr-2'}},
                {'component': 'span', 'text': f'检测站点（{len(site_names)}）'}
            ]
        })
        cards.append({'component': 'VDivider', 'props': {'class': 'mb-2'}})
        cards.append({'component': 'div', 'props': {'class': 'd-flex flex-wrap align-center'}, 'content': chips})
        return cards

    def stop_service(self):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown()
            self._scheduler = None

    def post_message(self, channel=None, mtype=None, title=None, text=None, image=None, link=None, userid=None):
        """
        发送消息
        """
        try:
            from app.schemas import Notification
            self.chain.post_message(Notification(
                channel=channel, mtype=mtype, title=title, text=text,
                image=image, link=link, userid=userid
            ))
           # logger.info("发送通知成功")
        except Exception as e:
            logger.error(f"推送通知失败: {e}") 