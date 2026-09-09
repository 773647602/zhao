"""
qmt_agent: 本地交易代理与行情上报

功能：
  - WebSocket 连接云端，接收交易指令
  - 调用本地 miniQMT xt_trader 执行下单/撤单
  - 监听 miniQMT 回调（成交/持仓/资金变动），实时回传云端
  - 行情上传（模式B）：订阅本地行情并推送到云端
  - 心跳保活，断线自动重连
"""

import json
import time
import logging
import threading
from typing import Optional, Callable
from urllib.parse import quote
from websocket import WebSocketApp, WebSocketConnectionClosedException


class WsClient:
    """WebSocket 客户端，支持自动重连"""

    def __init__(
        self,
        server_url: str,
        api_key: str,
        account_id: str = "",
        platform: str = "win",
        host: str = "",
    ):
        self.server_url = server_url
        self.api_key = api_key
        self.account_id = account_id
        self.platform = platform
        self.host = host
        self.ws: Optional[WebSocketApp] = None
        self.on_message: Optional[Callable[[dict], None]] = None
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 30
        self.logger = logging.getLogger("qmt_agent.ws")

    def connect(self):
        self._running = True
        url = f"{self.server_url}/ws/agent?token={quote(self.api_key, safe='')}"
        if self.account_id:
            url += f"&account_id={quote(str(self.account_id), safe='')}"
        if self.platform:
            url += f"&platform={quote(self.platform, safe='')}"
        if self.host:
            url += f"&host={quote(self.host, safe='')}"
        self.logger.info(f"连接云端: {self.server_url}/ws/agent "
                         f"[account={self.account_id} platform={self.platform} host={self.host}]")
        self.ws = WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_ws_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        thread.start()

    def send(self, data: dict):
        if self.ws:
            try:
                self.ws.send(json.dumps(data, ensure_ascii=False, default=str))
            except WebSocketConnectionClosedException:
                self.logger.warning("WS 发送失败，连接已关闭")

    def stop(self):
        self._running = False
        if self.ws:
            self.ws.close()

    def _on_open(self, ws):
        self.logger.info("WebSocket 已连接")
        self._reconnect_delay = 1

    def _on_ws_message(self, ws, message: str):
        try:
            data = json.loads(message)
            self.logger.debug(f"收到消息: {data.get('type')}")
            if self.on_message:
                self.on_message(data)
        except json.JSONDecodeError:
            self.logger.error(f"无法解析消息: {message[:200]}")

    def _on_error(self, ws, error):
        self.logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        self.logger.warning(f"WebSocket 断开: code={code} msg={msg}")
        if not self._running:
            return
        delay = min(self._reconnect_delay, self._max_reconnect_delay)
        self.logger.info(f"{delay}s 后重连...")
        time.sleep(delay)
        self._reconnect_delay *= 2
        self.connect()
