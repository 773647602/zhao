# -*- coding: utf-8 -*-
# QMT 选项配置路由 -- REST
"""
GET  /api/qmt/config   -- 读配置 (saved 表单值 + env 当前生效值)
POST /api/qmt/config   -- 保存配置 (写 config/qmt_bridge.json)
POST /api/qmt/ping     -- 测试连通 (按保存的 transport/host/port 连大 QMT 桥)

"测试连通"会把配置按 bridge 客户端格式渲染成
config/bigqmt_signal_trader_client_config.py, 然后走 xt_list 客户端真实 ping.
桥没启动 / 依赖缺失 / 账号没填时, 一律返回友好中文错误.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi import APIRouter

from lib.paths import PROJECT_ROOT, CONFIG_DIR
from lib.qmt_settings import load, save, env_values

router = APIRouter()

# 大 QMT 桥接客户端源码目录 (third_party/xtquant_big_convert/src)
BRIDGE_SRC = PROJECT_ROOT / "third_party" / "xtquant_big_convert" / "src"
CLIENT_CONFIG_MODULE = "bigqmt_signal_trader_client_config"
CLIENT_CONFIG_FILE = CONFIG_DIR / f"{CLIENT_CONFIG_MODULE}.py"


@router.get("/config")
def get_config():
    return {"saved": load(), "env": env_values()}


@router.post("/config")
def post_config(body: dict):
    cfg = save(body)
    return {"ok": True, "message": "配置已保存", "saved": cfg}


def _ensure_client_config(cfg: dict) -> None:
    """把表单配置渲染成 bridge 客户端配置文件 (写进 config/)."""
    try:
        from bigqmt_signal_trader import init_config
    except ImportError as exc:
        raise RuntimeError(
            "找不到大 QMT 桥客户端 bigqmt_signal_trader "
            f"(import 失败: {exc}); 请确认 third_party/xtquant_big_convert 完整存在") from exc

    answers = {
        "account_id": cfg.get("account_id") or "",
        "account_type": cfg.get("account_type") or "STOCK",
        "transport": cfg.get("transport") or "zmq",
        "host": cfg.get("host") or "127.0.0.1",
        "port": int(cfg.get("port") or 0),
        "db": int(cfg.get("db") or 5),
        "username": cfg.get("username") or "",
        "password": cfg.get("password") or "",
        "allow_order_methods": bool(cfg.get("allow_order_methods")),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_CONFIG_FILE.write_text(
        init_config.render_client_config(answers), encoding="utf-8")


def _check_deps(cfg: dict) -> None:
    """缺依赖时抛友好错误, 附 tsinghua 源安装命令."""
    import importlib.util
    # transport -> (import 模块名, pip 包名)
    dep_map = {"zmq": ("zmq", "pyzmq"), "redis": ("redis", "redis")}
    module_name, pip_name = dep_map.get(cfg.get("transport", "zmq"), ("zmq", "pyzmq"))
    if importlib.util.find_spec(module_name) is None:
        raise RuntimeError(
            f"缺少依赖 {pip_name}. 请先安装 (清华源):\n"
            f"`.venv\\Scripts\\python.exe -m pip install {pip_name} "
            f"-i https://pypi.tuna.tsinghua.edu.cn/simple`")
    if importlib.util.find_spec("xtquant") is None:
        raise RuntimeError(
            "当前进程的 sys.path 里没有 xtquant. 请在 QMT 的 python 环境下运行本系统")


@router.post("/ping")
def ping():
    # 优先: QMT 文件桥 (编辑器里跑文件桥, 只需 run_time + 文件读写, 不受沙箱限制)
    try:
        from lib import qmt_file_bridge as fb
        t0 = time.time()
        pong = fb._request("ping", timeout=3.0)
        latency_ms = round((time.time() - t0) * 1000, 1)
        acc = (pong or {}).get("account") or load().get("account_id") or ""
        return {"ok": True,
                "message": f"连通成功 (文件桥, account={acc})",
                "latency_ms": latency_ms, "transport": "file"}
    except Exception as file_err:
        file_hint = str(file_err)

    # 次选: 纯 socket 桥 (QMT 编辑器里运行 QMT_SOCKET_BRIDGE.py, 不依赖 pyzmq)
    try:
        from lib import qmt_socket_bridge as sb
        t0 = time.time()
        pong = sb._request("ping", timeout=3.0)
        latency_ms = round((time.time() - t0) * 1000, 1)
        acc = (pong or {}).get("account") or load().get("account_id") or ""
        return {"ok": True,
                "message": f"连通成功 (socket 桥, account={acc})",
                "latency_ms": latency_ms, "transport": "socket"}
    except Exception as sock_err:
        sock_hint = str(sock_err)

    cfg = load()
    account = (cfg.get("account_id") or "").strip()
    if not account:
        return _fail(f"QMT 桥未就绪 (文件桥: {file_hint}); 且未填写资金账号")
    if not cfg.get("port"):
        return _fail("端口为空, 请先保存配置 (zmq 端口会按账号自动推导)")

    # 把 bridge 源码目录与 config/ 加进 sys.path (导入客户端时需要)
    for path in (BRIDGE_SRC, CONFIG_DIR):
        sp = str(path)
        if path.exists() and sp not in sys.path:
            sys.path.insert(0, sp)

    try:
        _check_deps(cfg)
        _ensure_client_config(cfg)
    except Exception as exc:
        return _fail(f"socket 桥未就绪 ({sock_hint}); zmq 配置异常: {exc}")

    try:
        from bigqmt_signal_trader import xtquant_compat as xt
        xt.configure(account_id=account or None, redis_config={
            "transport": cfg.get("transport", "zmq"),
            "host": cfg.get("host") or "127.0.0.1",
            "port": int(cfg.get("port") or 0),
            "db": int(cfg.get("db") or 5),
            "username": cfg.get("username") or "",
            "password": cfg.get("password") or "",
        })
        t0 = time.time()
        pong = xt.get_default_client().call("ping")
        latency_ms = round((time.time() - t0) * 1000, 1)
        msg = _describe_pong(pong)
        return {"ok": True, "message": msg, "latency_ms": latency_ms}
    except Exception as exc:
        # 桥未启动 / 连接被拒 / RPC 超时都会走到这里
        hint = _hint_for(cfg, exc)
        detail = (f"QMT 桥未就绪 (文件桥: {file_hint}; "
                  f"socket 桥: {sock_hint}); "
                  f"zmq 桥失败: {type(exc).__name__}: {exc}")
        return _fail(f"{detail}\n{hint}" if hint else detail)


def _describe_pong(pong) -> str:
    """把 ping 响应翻译成一句话."""
    if isinstance(pong, dict):
        acc = pong.get("account_id") or pong.get("account") or ""
        ver = pong.get("version") or ""
        return f"连通成功 (account={acc}, version={ver})"
    return f"连通成功 (响应: {str(pong)[:80]})"


def _hint_for(cfg: dict, exc: Exception) -> str:
    """常见失败原因给出手把手的提示."""
    text = (type(exc).__name__ + " " + str(exc)).lower()
    if "refused" in text or "111" in text or "连接" in text:
        return ("提示: 连接被拒, 通常是桥还没启动或端口不对。"
                "请先在 QMT 里加载大 QMT 桥策略, 确认它绑定的端口与这里填的一致。")
    if "timeout" in text or "超时" in text:
        return ("提示: 请求超时。请确认 QMT 桥已启动, 且防火墙放行了该端口。")
    return ""


def _fail(message: str) -> dict:
    return {"ok": False, "message": message}