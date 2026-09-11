# -*- coding: utf-8 -*-
# 25-AI量化系统 回测路由
"""
GET  /api/backtest/ping              -- 健康检查 (含 MySQL 是否可连)
GET  /api/backtest/strategies        -- 可选策略列表（strategy_registry）
GET  /api/backtest/pool-meta         -- 选股池元数据 (策略名 + 各策略交易日), 批量回测下拉用
POST /api/backtest/batch             -- 批量回测 (异步任务): 两种模式 (选股池股票 / 日线数据股票)
GET  /api/backtest/batch/{task_id}   -- 轮询批量回测任务进度/结果

请求示例:
    POST /api/backtest/batch
    {"mode":"pool","selection_strategy":"weak_to_strong","trade_date":"2026-09-07",
     "strategy":"grid_classic","start":"2026-09-08","end":"2026-09-08"}
    {"mode":"daily","codes":"600519.SH,000001.SZ","strategy":"grid_classic",
     "start":"2024-01-01","end":"2025-12-31"}
"""

from __future__ import annotations
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body

from lib.paths import setup_sys_path
setup_sys_path()

from lib.backtest_data import mysql_available, get_stock_name
from lib.backtest_engine import run_backtest
from lib.strategy_registry import list_strategies, list_groups

router = APIRouter()


# ============================================================
# 工具
# ============================================================

def _normalize_code(code: str) -> str:
    """股票代码归一化:  600519 -> 600519.SH; 002432 -> 002432.SZ"""
    s = (code or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("60", "68", "90", "11")):
            return f"{s}.SH"
        return f"{s}.SZ"
    return s


# ============================================================
# 端点
# ============================================================

@router.get("/ping")
def backtest_ping():
    """健康检查 + 数据源探活"""
    return {
        "ok": True,
        "module": "backtest",
        "mysql_available": mysql_available(),
        "strategies": [s["name"] for s in list_strategies()],
        "hint": "POST /api/backtest/batch {mode, strategy, start, end} 批量回测; "
                "GET /api/backtest/batch/{task_id} 轮询进度",
    }


@router.get("/strategies")
def backtest_strategies():
    """可选策略列表 (按分组), 前端下拉用 -- 复用 registry"""
    return {
        "ok": True,
        "groups": list_groups(),
        "list": list_strategies(),
    }


# ============================================================
# 批量回测 (异步任务 + 前端轮询)
# ============================================================

_BATCH_TASKS: Dict[str, Dict[str, Any]] = {}   # task_id -> 任务状态
_BATCH_LOCK = threading.Lock()


def _batch_set(task_id: str, **kwargs) -> None:
    with _BATCH_LOCK:
        t = _BATCH_TASKS.setdefault(task_id, {})
        t.update(kwargs)
        t["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fetch_pool_codes(selection_strategy: str, trade_date: Optional[str]) -> List[str]:
    """从选股池取某策略 (可选交易日) 的股票代码, 保持池内顺序 (rank_no)"""
    from lib.selection_store import query_selection_pool
    rows = query_selection_pool(strategy=selection_strategy, trade_date=trade_date or None)
    return [str(r["stock_code"]) for r in rows]


def _fetch_daily_codes(codes_input: str, max_stocks: int) -> List[str]:
    """日线数据模式取股票: 用户指定列表(逗号分隔) 或 全市场有日线的股票(上限 max_stocks)"""
    import pymysql
    from lib.backtest_data import _db_config
    given = [c.strip().upper() for c in (codes_input or "").replace("，", ",").split(",") if c.strip()]
    if given:
        return [_normalize_code(c) for c in given]
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily ORDER BY stock_code")
        codes = [str(r[0]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    if len(codes) > max_stocks:
        codes = codes[:max_stocks]
    return codes


def _summarize_batch(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """整体盈亏: 单只盈亏(最终市值-初始资金) 直接相加 (正盈利+负盈利)"""
    pnls = [r["pnl"] for r in rows]
    win = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]
    return {
        "total_pnl":        round(sum(pnls), 2),            # 整体盈亏 = 正+负
        "positive_pnl":     round(sum(win), 2),             # 正盈利合计
        "negative_pnl":     round(sum(loss), 2),            # 负盈利合计
        "win_count":        len(win),
        "loss_count":       len(loss),
        "flat_count":       len(flat),
        "stock_count":      len(pnls),
        "avg_total_return": round((sum(r["total_return"] for r in rows) / len(rows)) if rows else 0.0, 6),
    }


def _run_batch_worker(task_id: str, payload: Dict[str, Any]) -> None:
    """后台线程: 逐只回测, 更新进度; 完成后写汇总结果"""
    try:
        codes: List[str] = payload["codes"]
        strategy = payload["strategy"]
        start, end = payload["start"], payload["end"]
        initial_cash = payload.get("initial_cash")
        total = len(codes)
        rows: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        _batch_set(task_id, status="running",
                   progress={"done": 0, "total": total}, rows=[], failed=[], summary=None)
        for i, code in enumerate(codes):
            if _BATCH_TASKS.get(task_id, {}).get("cancel"):
                _batch_set(task_id, status="cancelled", progress={"done": i, "total": total})
                return
            try:
                res = run_backtest(stock_code=code, strategy_name=strategy,
                                   start_date=start, end_date=end,
                                   initial_cash=initial_cash)
            except Exception as e:
                failed.append({"code": code, "error": f"{type(e).__name__}: {e}"})
                _batch_set(task_id, progress={"done": i + 1, "total": total})
                continue
            if not res.get("ok"):
                failed.append({"code": code, "error": res.get("message", "?")})
                _batch_set(task_id, progress={"done": i + 1, "total": total})
                continue
            m = res["metrics"]
            rows.append({
                "stock_code":    code,
                "name":          res.get("stock_name") or "",
                "pnl":           round(float(m["final_value"]) - float(m["initial_cash"]), 2),
                "total_return":  m["total_return"],
                "annual_return": m["annual_return"],
                "max_drawdown":  m["max_drawdown"],
                "sharpe":        m["sharpe_ratio"],
                "win_rate":      m["win_rate"],
                "trades":        m["total_trades"],
                "final_value":   m["final_value"],
            })
            _batch_set(task_id, progress={"done": i + 1, "total": total},
                       rows=list(rows), failed=list(failed))
        _batch_set(task_id, status="done",
                   progress={"done": total, "total": total},
                   rows=list(rows), failed=list(failed),
                   summary=_summarize_batch(rows))
    except Exception as e:
        _batch_set(task_id, status="error", error=f"{type(e).__name__}: {e}")


@router.get("/pool-meta")
def backtest_pool_meta():
    """选股池元数据: 池内策略名 + 各策略交易日, 批量回测「选股池」模式下拉用"""
    import pymysql
    from lib.backtest_data import _db_config
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT strategy, trade_date, COUNT(*) FROM trade_selection_pool "
                    "GROUP BY strategy, trade_date ORDER BY strategy, trade_date DESC")
        data = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    dates: Dict[str, List[str]] = {}
    strategies: List[str] = []
    for strat, td, cnt in data:
        td = str(td)
        if strat not in strategies:
            strategies.append(strat)
        dates.setdefault(strat, []).append({"trade_date": td, "count": int(cnt)})
    return {"ok": True, "strategies": strategies, "dates": dates}


@router.post("/batch")
def backtest_batch_start(payload: Dict[str, Any] = Body(default={})):
    """批量回测 (异步): 对一组股票逐只用同一策略回测, 输出单只盈亏 + 整体盈亏

    body: {
        "mode": "pool" | "daily",
        # pool 模式:
        "selection_strategy": "weak_to_strong",
        "trade_date":         "2026-09-07" (可选; 缺省=该策略全部交易日候选),
        # daily 模式:
        "codes":  "600519.SH,000001.SZ" (可选; 缺省=全市场有日线股票, 上限 max_stocks),
        "max_stocks": 300 (daily 缺省全市场时的上限),
        # 通用:
        "strategy": "grid_classic",         # 交易策略 (registry)
        "start": "YYYY-MM-DD",
        "end":   "YYYY-MM-DD",
        "initial_cash": 1000000        # 可选
    }
    返回 {task_id}; 用 GET /api/backtest/batch/{task_id} 轮询进度.
    """
    payload = payload or {}
    mode = str(payload.get("mode", "pool")).strip().lower()
    strategy = str(payload.get("strategy", "")).strip()
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()

    if mode not in ("pool", "daily"):
        return {"ok": False, "message": "mode 必须为 pool 或 daily"}
    if not strategy:
        return {"ok": False, "message": "strategy 不能为空"}
    if not start or not end:
        return {"ok": False, "message": "start / end 不能为空 (YYYY-MM-DD)"}
    try:
        from datetime import datetime
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "message": "日期格式应为 YYYY-MM-DD"}
    if start > end:
        return {"ok": False, "message": "起始日期晚于结束日期"}

    # 确定股票清单
    try:
        if mode == "pool":
            sel = str(payload.get("selection_strategy", "")).strip()
            if not sel:
                return {"ok": False, "message": "选股池模式需指定 selection_strategy"}
            codes = _fetch_pool_codes(sel, str(payload.get("trade_date", "")).strip() or None)
            if not codes:
                return {"ok": False, "message": f"选股池中无策略 {sel} 的候选股票"}
        else:
            codes = _fetch_daily_codes(str(payload.get("codes", "")),
                                       int(payload.get("max_stocks") or 300))
            if not codes:
                return {"ok": False, "message": "日线数据中没有可回测的股票"}
    except Exception as e:
        return {"ok": False, "message": f"取股票清单失败: {type(e).__name__}: {e}"}

    task_id = uuid.uuid4().hex[:12]
    job = {"codes": codes, "strategy": strategy, "start": start, "end": end,
           "initial_cash": payload.get("initial_cash")}
    _batch_set(task_id, status="queued", payload=job,
               progress={"done": 0, "total": len(codes)},
               rows=[], failed=[], summary=None)
    t = threading.Thread(target=_run_batch_worker, args=(task_id, job), daemon=True)
    t.start()
    return {"ok": True, "task_id": task_id, "mode": mode, "stock_count": len(codes),
            "message": f"批量回测已启动: {len(codes)} 只股票"}


@router.get("/batch/{task_id}")
def backtest_batch_status(task_id: str):
    """轮询批量回测任务状态: {status, progress, rows, failed, summary}"""
    with _BATCH_LOCK:
        t = _BATCH_TASKS.get(task_id)
        if t is None:
            return {"ok": False, "message": f"任务 {task_id} 不存在"}
        out = {k: v for k, v in t.items() if k != "payload"}
        # payload 里回传股票清单 (供前端显示本次回测的股票集合)
        out["stock_count"] = len((t.get("payload") or {}).get("codes") or [])
        out["task_id"] = task_id
        return {"ok": True, **out}
