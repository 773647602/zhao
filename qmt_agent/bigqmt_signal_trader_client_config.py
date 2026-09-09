# -*- coding: utf-8 -*-
"""qmt_agent 客户端配置 —— 连接国金证券大QMT RPC 桥（ZMQ 无 Redis）。

此文件需位于 sys.path 上（qmt_agent 目录即是），bigqmt_signal_trader
在 import 时会自动加载它。transport=zmq 时客户端直连
tcp://127.0.0.1:15615（端口由账号推导），不连接 Redis。
"""

BIGQMT_ACCOUNT_ID = "8890809055"
BIGQMT_ACCOUNT_TYPE = "STOCK"
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0

# 传输选 zmq。ZMQ 地址由账号派生：15560 + int('8890809055') % 100
# = tcp://127.0.0.1:15615，与服务端绑定端口一致。
BIGQMT_REDIS_CONFIG = {
    "transport": "zmq",
}