# -*- coding: utf-8 -*-
# QMT 文件轮询桥客户端 (纯标准库, 与 lib/qmt_file_bridge_server.py 配套)
"""
背景:
    QMT 策略编辑器沙箱缺 _ctypes (pyzmq 跑不了), 且 socket.bind / 后台线程也可能被拦。
    文件桥只用 QMT 策略一定能做的两件事: run_time 回调 + 文件读写。

工作方式 (共享目录 RPC):
    本端  -> 写 D:\\qmt_file_bridge\\req.json   {"sid","id","method","params"}
    QMT   -> run_time("adjust",100ms) 读 req, 调 ContextInfo,
            写 D:\\qmt_file_bridge\\resp.json   {"sid","id","ok","result"/"error"}
    QMT   -> 每次 adjust 顺带写 hb.json (心跳), 用于判断桥是否存活

对外接口与 lib/qmt_socket_bridge.py 完全一致, 因此上层 backtest_data / live_loop
无需改动。sid 用 "CASE-{pid}-{启动时间}" 区分不同进程, 避免多进程互抢响应。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

import pandas as pd

BRIDGE_DIR = r"D:\qmt_file_bridge"
REQ_FILE = "req.json"
RESP_FILE = "resp.json"
HB_FILE = "hb.json"

# 本进程唯一会话号
_SID = "CASE-%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])
_next_id = 0
_lock = threading.Lock()  # req/resp 文件是共享单槽, 同进程内必须串行

_avail_cache = {"ts": 0.0, "ok": False}


class QmtBridgeError(RuntimeError):
    pass


def _write(path: str, obj: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _request(method: str, params: Optional[dict] = None,
             timeout: float = 12.0, poll: float = 0.05):
    """写请求 -> 轮询响应 (匹配 sid+id). 同进程内串行 (共享单槽文件)."""
    global _next_id
    if not os.path.isdir(BRIDGE_DIR):
        raise QmtBridgeError(f"桥目录不存在: {BRIDGE_DIR} (QMT 文件桥未运行)")
    with _lock:
        _next_id += 1
        rid = _next_id
        resp_path = os.path.join(BRIDGE_DIR, RESP_FILE)
        # 清掉旧响应, 避免读到上一次
        try:
            if os.path.exists(resp_path):
                os.remove(resp_path)
        except Exception:
            pass
        _write(os.path.join(BRIDGE_DIR, REQ_FILE),
               {"sid": _SID, "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = _read(resp_path)
            if (isinstance(resp, dict) and resp.get("sid") == _SID
                    and resp.get("id") == rid):
                if not resp.get("ok"):
                    raise QmtBridgeError(f"桥返回错误: {resp.get('error')}")
                return resp.get("result")
            time.sleep(poll)
        raise QmtBridgeError(f"{method} 超时 {timeout}s (QMT 端 adjust 未消费请求?)")


def bridge_available(ttl: float = 20.0) -> bool:
    """桥是否可用: 心跳新鲜 (QMT 在跑) + ping 通. 结果缓存 ttl 秒."""
    now = time.time()
    if now - _avail_cache["ts"] < ttl:
        return _avail_cache["ok"]
    ok = False
    try:
        hb = _read(os.path.join(BRIDGE_DIR, HB_FILE))
        fresh = False
        if isinstance(hb, dict) and hb.get("hb"):
            try:
                ts = time.mktime(time.strptime(hb["hb"], "%Y-%m-%d %H:%M:%S"))
                fresh = (now - ts) < 10.0
            except Exception:
                fresh = False
        if fresh:
            _request("ping", timeout=3.0)
            ok = True
    except Exception:
        ok = False
    _avail_cache.update(ts=now, ok=ok)
    return ok


def get_full_tick(codes) -> dict:
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
        raise QmtBridgeError(f"桥无数据: {stock_code}")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_index()
    if df.empty:
        raise QmtBridgeError(f"桥数据为空: {stock_code}")
    return df
