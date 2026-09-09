"""
行情数据上传（模式B）

订阅本地 miniQMT 实时行情，定期批量推送到所有云端连接。
"""

import logging
import time
import threading
from typing import Optional, List


class MarketDataUploader:
    """
    本地行情订阅 → 批量推送到所有云端 WebSocket 连接。

    用法：
        uploader = MarketDataUploader(connection_manager, stock_codes)
        uploader.start()
    """

    def __init__(self, conn_mgr, stock_codes: List[str], interval: int = 10):
        self.conn_mgr = conn_mgr  # ConnectionManager 实例
        self.stock_codes = stock_codes
        self.interval = interval  # 上报间隔（秒）
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._xtdata = None
        self._tick_cache = {}  # code → last tick
        self.logger = logging.getLogger("qmt_agent.market")

    def start(self):
        """启动行情订阅与推送线程"""
        try:
            from bigqmt_signal_trader.xtquant_compat import get_default_client, BigQmtXtData
            self._xtdata = BigQmtXtData(get_default_client())
        except ImportError:
            self.logger.warning("未安装 bigqmt_signal_trader，行情上传不可用")
            return

        if not self.stock_codes:
            self.logger.info("无订阅股票，行情上传跳过")
            return

        self._running = True
        self._subscribe()
        self._thread = threading.Thread(target=self._upload_loop, daemon=True)
        self._thread.start()
        self.logger.info(f"行情上传已启动: {len(self.stock_codes)} 只股票, {self.interval}s 间隔")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def update_codes(self, codes: List[str]):
        """热更新订阅列表"""
        self.stock_codes = codes
        self._subscribe()

    def _subscribe(self):
        if not self._xtdata:
            return
        try:
            # 订阅全推行情（tick + 1分钟 K 线）
            self._xtdata.subscribe_quote(self.stock_codes, period='tick')
            self._xtdata.subscribe_quote(self.stock_codes, period='1m')
            self.logger.info(f"已订阅行情: {len(self.stock_codes)} 只")
        except Exception as e:
            self.logger.error(f"行情订阅失败: {e}")

    def _upload_loop(self):
        while self._running:
            time.sleep(self.interval)
            if not self._running:
                break
            self._collect_and_send()

    def _collect_and_send(self):
        """采集最新行情并广播到所有云端连接"""
        if not self._xtdata:
            return

        quotes = {}
        for code in self.stock_codes:
            try:
                tick = self._xtdata.get_full_tick([code])
                if tick and code in tick:
                    t = tick[code]
                    quotes[code] = {
                        "last_price": t.get("lastPrice", 0),
                        "open": t.get("open", 0),
                        "high": t.get("high", 0),
                        "low": t.get("low", 0),
                        "volume": t.get("volume", 0),
                        "amount": t.get("amount", 0),
                        "time": t.get("time", ""),
                    }
            except Exception as e:
                self.logger.debug(f"获取 {code} 行情失败: {e}")

        if quotes:
            self.conn_mgr.broadcast({
                "type": "market_data",
                "data": quotes,
                "timestamp": time.time(),
            })
            self.logger.debug(f"行情广播: {len(quotes)} 条 → {self.conn_mgr.online_count} 个连接")