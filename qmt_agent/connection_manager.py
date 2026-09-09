"""
qmt_agent 连接管理器

管理多个 WebSocket 连接的生命周期，支持：
  - 根据 servers 配置列表创建/销毁 WsClient 实例
  - 向所有在线连接广播消息
  - 单个连接重启
  - 在线状态统计
"""

import logging
import threading
from typing import Dict, Optional, Callable

from ws_client import WsClient


class ConnectionManager:
    """管理多个 WsClient 实例，维护 name → WsClient 映射"""

    def __init__(self, config: dict):
        self.servers_config = config.get("servers", [])
        self.global_config = {
            k: v for k, v in config.items() if k != "servers"
        }
        self.clients: Dict[str, WsClient] = {}
        self._lock = threading.Lock()
        self.logger = logging.getLogger("qmt_agent.conn_mgr")

    def start_all(self, on_message: Optional[Callable] = None):
        """启动所有 enabled=true 的服务器连接"""
        for server_cfg in self.servers_config:
            if not server_cfg.get("enabled", True):
                self.logger.info(f"跳过已禁用的服务器: {server_cfg.get('name', '未命名')}")
                continue
            self._start_client(server_cfg, on_message)

    def _start_client(self, server_cfg: dict, on_message: Optional[Callable] = None):
        """创建并启动单个 WsClient"""
        name = server_cfg.get("name", server_cfg.get("server_url", "未命名"))
        server_url = server_cfg.get("server_url", "")
        api_key = server_cfg.get("api_key", "")
        account_id = str(server_cfg.get("account_id", ""))
        platform = self.global_config.get("platform", "win")
        host = self.global_config.get("host", "")

        if not server_url or not api_key:
            self.logger.warning(f"服务器 '{name}' 缺少 server_url 或 api_key，跳过")
            return

        client = WsClient(
            server_url=server_url,
            api_key=api_key,
            account_id=account_id,
            platform=platform,
            host=host,
        )

        if on_message:
            client.on_message = on_message

        client.connect()

        with self._lock:
            self.clients[name] = client

        self.logger.info(f"已启动连接: {name} → {server_url}")

    def stop_all(self):
        """关闭所有连接"""
        with self._lock:
            for name, client in list(self.clients.items()):
                try:
                    client.stop()
                except Exception as e:
                    self.logger.warning(f"关闭连接 '{name}' 失败: {e}")
            self.clients.clear()
        self.logger.info("所有连接已关闭")

    def stop_client(self, name: str):
        """关闭指定连接"""
        with self._lock:
            client = self.clients.pop(name, None)
        if client:
            client.stop()
            self.logger.info(f"已关闭连接: {name}")

    def restart_client(self, name: str, on_message: Optional[Callable] = None):
        """重启指定连接"""
        self.stop_client(name)
        for server_cfg in self.servers_config:
            if server_cfg.get("name") == name:
                self._start_client(server_cfg, on_message)
                return
        self.logger.warning(f"未找到服务器配置: {name}")

    def broadcast(self, data: dict):
        """向所有在线连接广播消息"""
        with self._lock:
            clients = list(self.clients.items())
        for name, client in clients:
            try:
                client.send(data)
            except Exception as e:
                self.logger.debug(f"向 '{name}' 发送消息失败: {e}")

    def send_to(self, name: str, data: dict):
        """向指定连接发送消息"""
        with self._lock:
            client = self.clients.get(name)
        if client:
            try:
                client.send(data)
            except Exception as e:
                self.logger.warning(f"向 '{name}' 发送消息失败: {e}")
        else:
            self.logger.debug(f"连接 '{name}' 不存在，无法发送消息")

    def get_client(self, name: str) -> Optional[WsClient]:
        """根据名称获取指定连接"""
        with self._lock:
            return self.clients.get(name)

    @property
    def online_count(self) -> int:
        """在线连接数"""
        with self._lock:
            return len(self.clients)

    @property
    def is_any_online(self) -> bool:
        """是否有任何在线连接"""
        return self.online_count > 0

    def get_status(self) -> list:
        """获取所有服务器连接状态"""
        result = []
        for server_cfg in self.servers_config:
            name = server_cfg.get("name", "未知")
            with self._lock:
                online = name in self.clients
            result.append({
                "name": name,
                "server_url": server_cfg.get("server_url", ""),
                "enabled": server_cfg.get("enabled", True),
                "online": online,
            })
        return result