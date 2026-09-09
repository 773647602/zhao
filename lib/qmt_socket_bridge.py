# -*- coding: utf-8 -*-
# QMT 纯 socket 桥接客户端
"""
qmt_socket_bridge -- 连接 QMT 策略编辑器里运行的 QMT_SOCKET_BRIDGE.py 服务端

背景:
    本机是大 QMT 全终端, 不提供标准 xtdata 数据服务 (58610 未监听);
    QMT 编辑器沙箱又缺 _ctypes, pyzmq / bigqmt_signal_trader 的 zmq 桥跑不起来。
    本模块用纯标准库 socket 实现长度前缀 JSON 协议, 与 lib/qmt_socket_bridge_server.py 配套。

协议:
    4 字节大端长度 + UTF-8 JSON
    请求  {"id": int, "method": str, "params": {...}}
    响应  {"id": int, "ok": true/false, "result"/"error": ...}

对外接口 (全部 fail-soft, 桥不可用时返回 None / 抛 QmtBridgeError):
    bridge_available()          -- 快速探测 (TCP 可连 + ping 通, 结果缓存 30s)
    get_full_tick(codes)        -- 实时盘口 {code: {...}}
    get_market_data_ex(...)     -- 历史 K 线 -> {code: DataFrame(index=DatetimeIndex)}
    load_daily_kline(code, ...) -- 日 K (列 open/high/low/close/volume), 与 backtest_data 同格式
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Optional

import pandas as pd

HOST = "127.0.0.1"
PORT = 15055
_TIMEOUT = 10.0

_lock = threading.Lock()
_sock: Optional[socket.socket] = None
_next_id = 0
_avail_cache = {"ts": 0.0, "ok": False}


class QmtBridgeError(RuntimeError):
    pass


def _reset():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
    _sock = None


def _ensure_conn() -> socket.socket:
    global _sock
    if _sock is not None:
        return _sock
    s = socket.create_connection((HOST, PORT), timeout=_TIMEOUT)
    s.settimeout(_TIMEOUT)
    _sock = s
    return s


def _request(method: str, params: Optional[dict] = None, timeout: float = _TIMEOUT):
    """发送一次 RPC; 断线自动重连一次."""
    global _next_id
    with _lock:
        for attempt in (0, 1):
            try:
                sock = _ensure_conn()
                _next_id += 1
                rid = _next_id
                body = json.dumps(
                    {"id": rid, "method": method, "params": params or {}},
                    ensure_ascii=False,
                ).encode("utf-8")
                sock.sendall(struct.pack(">I", len(body)) + body)
                head = _recv_exact(sock, 4)
                if head is None:
                    raise QmtBridgeError("桥接服务端关闭连接")
                (size,) = struct.unpack(">I", head)
                payload = _recv_exact(sock, size)
                if payload is None:
                    raise QmtBridgeError("响应截断")
                resp = json.loads(payload.decode("utf-8"))
                if not resp.get("ok"):
                    raise QmtBridgeError(f"桥接返回错误: {resp.get('error')}")
                return resp.get("result")
            except (OSError, QmtBridgeError) as err:
                _reset()
                if attempt == 1:
                    raise QmtBridgeError(f"{method} 失败: {err}") from err
                time.sleep(0.2)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def bridge_available(ttl: float = 30.0) -> bool:
    """桥是否可用 (TCP + ping), 结果缓存 ttl 秒, 避免每次取数都探测."""
    now = time.time()
    if now - _avail_cache["ts"] < ttl:
        return _avail_cache["ok"]
    try:
        _request("ping", timeout=3.0)
        _avail_cache.update(ts=now, ok=True)
    except Exception:
        _avail_cache.update(ts=now, ok=False)
    return _avail_cache["ok"]


def get_full_tick(codes) -> dict:
    """实时盘口: {code: {lastPrice, open, high, low, ...}}"""
    return _request("get_full_tick", {"codes": list(codes)}) or {}


def _payload_to_frame(payload: dict) -> pd.DataFrame:
    columns = payload.get("columns") or []
    records = payload.get("records") or []
    index = payload.get("index") or []
    df = pd.DataFrame(records, columns=columns if columns else None)
    if index and len(index) == len(df):
        df.index = pd.to_datetime(index, format="%Y%m%d", errors="coerce")
    elif "stime" in df.columns:
        df.index = pd.to_datetime(df["stime"], format="%Y%m%d", errors="coerce")
        df.drop(columns=["stime"], inplace=True)
    return df


def get_market_data_ex(field_list, stock_list, period="1d",
                       start_time="", end_time="", count=-1,
                       dividend_type="none") -> dict:
    """历史行情 -> {code: DataFrame}"""
    result = _request("get_market_data_ex", {
        "field_list": list(field_list),
        "stock_list": list(stock_list),
        "period": period,
        "start_time": start_time,
        "end_time": end_time,
        "count": count,
        "dividend_type": dividend_type,
    }) or {}
    return {code: _payload_to_frame(payload)
            for code, payload in result.items()
            if isinstance(payload, dict)}


def load_daily_kline(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> pd.DataFrame:
    """日 K (与 lib.backtest_data.load_daily_kline 同格式: DatetimeIndex + OHLCV)"""
    sd = (start_date or "20200101").replace("-", "")[:8]
    ed = (end_date or "20991231").replace("-", "")[:8]
    frames = get_market_data_ex(
        field_list=["open", "high", "low", "close", "volume"],
        stock_list=[stock_code],
        period="1d", start_time=sd, end_time=ed,
    )
    df = frames.get(stock_code)
    if df is None or df.empty:
        raise QmtBridgeError(f"桥接无数据: {stock_code}")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_index()
    if df.empty:
        raise QmtBridgeError(f"桥接数据为空: {stock_code}")
    return df
