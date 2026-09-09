"""
qmt_agent 主程序入口

连接云端 WebSocket，接收交易指令并执行，同时上报行情和账户状态。
支持同时连接多个云端服务器。

用法:
    python agent.py --config config.yaml
    python agent.py --server wss://your-cloud.com --api-key xxx --qmt-path "..."
    python agent.py --config-gui  # 打开配置窗口
"""

import argparse
import logging
import os
import signal
import sys
import yaml

from connection_manager import ConnectionManager
from message_router import MessageRouter
from market_data import MarketDataUploader
from trader import QmtTrader


def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)


def parse_args():
    parser = argparse.ArgumentParser(description="AI 量化本地交易代理")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--server", help="云端 WebSocket 地址 (覆盖第一个 server 配置)")
    parser.add_argument("--api-key", help="API Key (覆盖第一个 server 配置; 也可用环境变量 QMT_API_KEY)")
    parser.add_argument("--qmt-path", help="miniQMT 路径 (覆盖全局配置)")
    parser.add_argument("--account-id", help="资金账户 ID (覆盖第一个 server 配置)")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--stocks", nargs="*", default=[], help="初始订阅股票列表")
    parser.add_argument("--config-gui", action="store_true", help="打开配置窗口")
    return parser.parse_args()


def load_config(args) -> dict:
    config = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    # 向后兼容：检测旧格式（无 servers 字段），自动转换为新格式
    if "servers" not in config:
        old_server = {
            "name": "默认服务器",
            "server_url": config.get("server_url", "ws://127.0.0.1:8000"),
            "api_key": config.get("api_key", ""),
            "account_id": config.get("account_id", ""),
            "enabled": True,
        }
        config = {
            "qmt_path": config.get("qmt_path", ""),
            "log_level": config.get("log_level", "INFO"),
            "host": config.get("host", "未命名"),
            "platform": config.get("platform", "win"),
            "servers": [old_server],
        }
        print("检测到旧版 config.yaml，已自动转换为多服务器格式")

    # CLI 参数覆盖
    if args.qmt_path:
        config["qmt_path"] = args.qmt_path
    if args.log_level:
        config["log_level"] = args.log_level

    config.setdefault("qmt_path", "")
    config.setdefault("host", "未命名")
    config.setdefault("platform", "win")

    # CLI 参数覆盖第一个 server
    if config.get("servers"):
        first = config["servers"][0]
        if args.server:
            first["server_url"] = args.server
        if args.api_key:
            first["api_key"] = args.api_key
        elif os.getenv("QMT_API_KEY"):
            first["api_key"] = os.getenv("QMT_API_KEY")
        if args.account_id:
            first["account_id"] = args.account_id

    # 全局 stocks 配置
    config["stocks"] = args.stocks if args.stocks else config.get("stocks", [])

    # 验证至少有一个 enabled server
    enabled_servers = [s for s in config.get("servers", []) if s.get("enabled", True)]
    if not enabled_servers:
        print("警告: 没有启用的服务器连接，请检查 config.yaml 中的 servers 配置")

    return config


class QmtAgent:
    """多服务器连接代理"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("qmt_agent")

        # 连接管理器（管理多个 WsClient）
        self.conn_mgr = ConnectionManager(config)

        # 交易封装（唯一实例）。
        # 账号优先取顶层 account_id（国金大QMT RPC 桥），缺省回退到第一个 server。
        servers = config.get("servers", [])
        account_id = str(config.get("account_id") or (servers[0]["account_id"] if servers else ""))
        # 兜底：允许 bigqmt_signal_trader 的 xtquant_compat 从环境变量读账号
        if account_id and not os.getenv("BIGQMT_ACCOUNT_ID"):
            os.environ["BIGQMT_ACCOUNT_ID"] = account_id
        self.trader = QmtTrader(config.get("qmt_path", ""), account_id)

        # 行情上传（共享实例，数据广播到所有连接）
        self.uploader = MarketDataUploader(self.conn_mgr, config.get("stocks", []))

        # 消息路由器（请求-回复到来源，事件-广播到所有）
        self.router = MessageRouter(self.conn_mgr, self.trader, self.uploader)

    def start(self):
        self.logger.info("AI 量化 QMT Agent 启动 (多服务器模式)")

        # 1. 连接 miniQMT
        self.trader.connect()

        # 2. 设置回调 → 广播到所有云端连接
        self.trader.on_order = lambda o: self.router.broadcast({
            "type": "order_report",
            **o,
        })
        self.trader.on_trade = lambda t: self.router.broadcast({
            "type": "trade_report",
            **t,
        })
        self.trader.on_asset = lambda a: self.router.broadcast({
            "type": "asset_report",
            **a,
        })

        # 3. 启动所有 WebSocket 连接
        def on_message_factory(ws_client):
            """为每个 WsClient 创建消息处理器闭包"""
            def handler(msg):
                self.router.route(ws_client, msg)
            return handler

        self.conn_mgr.start_all(on_message=None)

        # 为每个已创建的 client 设置消息处理器
        for name, client in self.conn_mgr.clients.items():
            client.on_message = on_message_factory(client)

        self.logger.info(f"已连接 {self.conn_mgr.online_count} 个服务器")

        # 4. 启动行情上传（模式B）
        self.uploader.start()

        # 5. 注册信号处理
        signal.signal(signal.SIGINT, self._on_shutdown)
        signal.signal(signal.SIGTERM, self._on_shutdown)

        # 保持主线程存活
        self.logger.info("Agent 运行中，按 Ctrl+C 退出...")
        while True:
            import time
            time.sleep(1)

    def stop(self):
        self.logger.info("Agent 正在关闭...")
        self.uploader.stop()
        self.trader.disconnect()
        self.conn_mgr.stop_all()
        self.logger.info("Agent 已关闭")

    def restart(self):
        """重启 Agent（重新加载配置）"""
        self.logger.info("Agent 正在重启...")
        self.stop()
        # 重新加载配置
        config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                new_config = yaml.safe_load(f) or {}
            # 向后兼容转换
            if "servers" not in new_config:
                old_server = {
                    "name": "默认服务器",
                    "server_url": new_config.get("server_url", "ws://127.0.0.1:8000"),
                    "api_key": new_config.get("api_key", ""),
                    "account_id": new_config.get("account_id", ""),
                    "enabled": True,
                }
                new_config = {
                    "qmt_path": new_config.get("qmt_path", ""),
                    "log_level": new_config.get("log_level", "INFO"),
                    "host": new_config.get("host", "未命名"),
                    "platform": new_config.get("platform", "win"),
                    "servers": [old_server],
                }
            self.config = new_config
            self.conn_mgr = ConnectionManager(new_config)
            self.uploader = MarketDataUploader(self.conn_mgr, new_config.get("stocks", []))
            self.router = MessageRouter(self.conn_mgr, self.trader, self.uploader)
        self.start()

    def _on_shutdown(self, signum, frame):
        self.logger.info(f"收到信号 {signum}，正在退出...")
        self.stop()
        sys.exit(0)


def main():
    args = parse_args()

    if args.config_gui:
        from config_gui import open_config_window
        open_config_window(args.config)
        return

    config = load_config(args)
    setup_logging(config.get("log_level", "INFO"))

    agent = QmtAgent(config)
    agent.start()


if __name__ == "__main__":
    main()