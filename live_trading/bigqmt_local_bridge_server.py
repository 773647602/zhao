# -*- coding: utf-8 -*-
# 本地独立服务端进程: 绑定 BIGQMT ZMQ 15055, 复用 bigqmt_signal_trader 的 ZmqTransport
"""
用途:
    在国金大 QMT 客户端没有挂载 bigqmt_signal_trader 策略时, 由本进程代理 15055 端口,
    让外部客户端(BigQmtRpcClient) 能连上并得到响应(至少 ping OK)。

说明:
    本进程属于"独立回环桥", 不连接 QMT 的 ContextInfo:
      - ping 等静态调用  -> 正常应答
      - get_full_tick / get_market_data_ex  -> 返回空/占位(不提供真实盘中行情)
      - 下单类(name/order_stock 等)        -> 返回 dry-run 拒绝, 绝不真下单
    要拿到真实行情与真实下单, 必须让 bigqmt_signal_trader 策略在 QMT 模型交易里运行。
    本脚本主要用于验证链路连通、页面/客户端不再超时。

启动(必须用项目 venv 的 python, 内含 pyzmq):
    .venv\\Scripts\\python.exe live_trading/bigqmt_local_bridge_server.py --port 15055
"""

from __future__ import annotations

import argparse
import time
import datetime as _dt

ACCOUNT_ID = "8890809055"


def _default_response(request, method, data=None, ok=True, error="", server_error=""):
    return {
        "schema_version": 1,
        "request_id": str((request or {}).get("request_id") or ""),
        "account_id": str((request or {}).get("account_id") or ACCOUNT_ID),
        "method": method,
        "ok": ok,
        "data": data,
        "error": error,
        "server_error": server_error,
        "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_on_request(account_id):
    """返回 on_request(request_dict) -> response_dict 的回调。"""
    def _on_request(request):
        method = str((request or {}).get("method") or "")
        acc = str((request or {}).get("account_id") or account_id)

        if method == "ping":
            return _default_response(request, method, data={"pong": True,
                                                            "local_bridge": True,
                                                            "acc": acc})

        # ---- 下单类: 一律拒绝, 防止独立桥误下单 ----
        if method in ("order_stock", "passorder", "submit_order",
                      "cancel_order", "credit_order", "repay_coin_stock"):
            return _default_response(
                request, method, ok=False,
                error="本地独立桥不支持下单, 请在 QMT 模型交易中运行 bigqmt_signal_trader",
            )

        # ---- 行情类: 占位, 不提供真实盘中行情 ----
        if method in ("get_full_tick", "get_market_data", "get_market_data_ex",
                      "get_local_data", "get_instrumentdetail"):
            return _default_response(request, method, data={})

        # 未知方法: 返回 ok=False 但进程不崩溃
        return _default_response(
            request, method, ok=False,
            error="本地桥未实现方法: %s" % method,
        )

    return _on_request


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地 BIGQMT 15055 独立回环桥")
    parser.add_argument("--port", type=int, default=15055,
                        help="绑定的 ZMQ 端口 (默认 15055)")
    parser.add_argument("--account", default=ACCOUNT_ID,
                        help="资金账号 (默认 %s)" % ACCOUNT_ID)
    args = parser.parse_args(argv)

    from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport

    transport = ZmqTransport(
        bind_address="tcp://127.0.0.1:%d" % args.port,
        account_id=args.account,
        print_prefix="[bigqmt_local_bridge]",
        recv_timeout_seconds=1.0,
    )

    def _on_request(request):
        res = build_on_request(args.account)(request)
        print("%s -> %s ok=%s"
              % (_dt.datetime.now().strftime("%H:%M:%S"),
                 (request or {}).get("method"),
                 res.get("ok")))
        return res

    print("[bigqmt_local_bridge] starting, bind=tcp://127.0.0.1:%d account=%s"
          % (args.port, args.account))
    transport.start_receiving(_on_request)
    print("[bigqmt_local_bridge] ready. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        print("[bigqmt_local_bridge] stopping")
    finally:
        transport.stop()


if __name__ == "__main__":
    main()