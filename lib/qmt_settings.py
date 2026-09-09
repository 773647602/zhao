# -*- coding: utf-8 -*-
# QMT 选项配置 -- 读写 config/qmt_bridge.json
"""
页面 /qmt 的后端数据层:
    default()/load()/save()  管理保存在 config/qmt_bridge.json 的桥接参数
    env_values()             返回 .env 里"当前生效"的 QMT_PATH / ACCOUNT_ID

不改 .env (避免改完要重启进程才生效); 页面表单的配置落地到 JSON.
字段约定与 bigqmt_signal_trader/init_config.py 的 answers 保持一致:
    qmt_path / account_id / account_type / transport / host / port /
    db / username / password / allow_order_methods / qmt_python_dir
"""

from __future__ import annotations

import json
import os

from lib.paths import CONFIG_DIR

QMT_CONFIG_FILE = CONFIG_DIR / "qmt_bridge.json"

# 账号类型选项 (与 bridge 的 ACCOUNT_TYPES 一致, 只留常用选项)
ACCOUNT_TYPES = ("STOCK", "CREDIT", "FUTURE", "STOCK_OPTION")
# 传输方式
TRANSPORTS = ("zmq", "redis")


def _zmq_default_port(account_id) -> int:
    """zmq 端口按账号数字推导: 15000 + (账号数字 % 1000)"""
    digits = "".join(c for c in str(account_id) if c.isdigit())
    if not digits:
        return 15563
    return 15000 + (int(digits) % 1000)


def default() -> dict:
    """默认配置"""
    return {
        "qmt_path": "",
        "account_id": "",
        "account_type": "STOCK",
        "transport": "zmq",
        "host": "127.0.0.1",
        "port": 15563,
        "db": 5,
        "username": "",
        "password": "",
        "allow_order_methods": False,
        "qmt_python_dir": "",
    }


def load() -> dict:
    """读回配置, 缺失字段用默认补齐"""
    cfg = default()
    if QMT_CONFIG_FILE.exists():
        try:
            data = json.loads(QMT_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (json.JSONDecodeError, OSError):
            # 配置文件损坏时退回默认, 不强抛 (页面仍可编辑重新保存)
            pass
    # 归一化: 类型不合法时回落默认
    if cfg["transport"] not in TRANSPORTS:
        cfg["transport"] = "zmq"
    if cfg["account_type"] not in ACCOUNT_TYPES:
        cfg["account_type"] = "STOCK"
    try:
        cfg["port"] = int(cfg.get("port") or 0)
        cfg["db"] = int(cfg.get("db") or 0)
    except (TypeError, ValueError):
        cfg["port"] = _zmq_default_port(cfg["account_id"])
        cfg["db"] = 5
    return cfg


def save(cfg: dict) -> dict:
    """合并用户提交并保存到 config/qmt_bridge.json"""
    data = load()
    data.update({k: v for k, v in cfg.items() if k in data})
    # 端口/账号为空时按账号推导 (zmq 场景下省得手填)
    if not data.get("port"):
        data["port"] = _zmq_default_port(data["account_id"])
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    QMT_CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def env_values() -> dict:
    """返回 .env / 环境里"当前生效"的 QMT_PATH 与 ACCOUNT_ID (系统状态页同源)"""
    # 只读一次点位, 不缓存: 配置文件可能在运行中改动
    return {
        "qmt_path_env": os.environ.get("QMT_PATH", "") or None,
        "account_id_env": os.environ.get("ACCOUNT_ID", "") or None,
    }