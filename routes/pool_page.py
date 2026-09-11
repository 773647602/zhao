# -*- coding: utf-8 -*-
# 选股池路由 -- REST
"""
GET  /api/pool/list  -- 返回当前选股池, 按策略分组 (每个策略自己选出的候选股)
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body
from typing import Any, Dict, Optional

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE
setup_sys_path()

from lib.strategy_runner import (
    load_selection_config, get_selection_state,
)
from lib.selection_store import query_selection_pool
from lib.strategy_registry import get_strategy

router = APIRouter()


_NAME_CACHE = {"ts": 0.0, "data": {}}


def _name_map_cached() -> dict:
    """权威名称映射(trade_stock_basic 全表)缓存: 名称稳定, 5 分钟内复用避免每请求重查."""
    import time
    now = time.time()
    if now - _NAME_CACHE["ts"] < 300:
        return _NAME_CACHE["data"]
    try:
        from lib.selection_engine import _stock_name_map
        _NAME_CACHE["data"] = _stock_name_map()
    except Exception:
        _NAME_CACHE["data"] = {}
    _NAME_CACHE["ts"] = now
    return _NAME_CACHE["data"]


def _load_state() -> dict:
    if not OUTPUTS_LIVE_STATE.exists():
        return {}
    try:
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _prev_market_volume() -> dict:
    """昨日全市场量能 (trade_market_volume): 取 trade_date < 今日 的最近一条.

    返回 {"trade_date", "total_amount", "text"} (total_amount 单位元, text 为格式化万亿);
    无数据/异常返回 {}。
    """
    import datetime as _dt
    return _prev_market_volume_by_date(_dt.date.today().strftime("%Y-%m-%d"))


_DAILY_INGEST_CACHE: dict = {}

_PMV_CACHE: dict = {}


def _latest_daily_ingest() -> dict:
    """最新日线入库情况 (trade_stock_daily): 最近入库交易日 + 该日入库股票数 + 完成度.

    返回 {"trade_date", "count", "expected", "pct", "complete"}:
        trade_date / count  最新一个已入库交易日及其行(股票)数
        expected            全市场参考量(约 5200, 沪深 A 股 + ETF)
        pct / complete      完成度百分比 及是否达到 expected (>=100%)
    无数据返回 {}。结果缓存, 避免每请求全表查询。
    """
    import time
    global _DAILY_INGEST_CACHE
    if _DAILY_INGEST_CACHE and time.time() - _DAILY_INGEST_CACHE.get("ts", 0) < 60:
        return _DAILY_INGEST_CACHE.get("data", {})

    out: dict = {}
    try:
        import datetime as _dt
        from lib.backtest_data import _db_config
        import pymysql
        conn = pymysql.connect(**_db_config())
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM trade_stock_daily")
            m = cur.fetchone()
            cnt = 0
            date_s = ""
            if m and m[0]:
                date_s = str(m[0])[:10]
                cur.execute(
                    "SELECT COUNT(*) FROM trade_stock_daily WHERE trade_date=%s",
                    (str(m[0])[:10],))
                c = cur.fetchone()
                cnt = int(c[0]) if c and c[0] else 0
            cur.close()
            if date_s:
                # 全市场参考量: 取该史上入库交易日已入库数的历史峰值, 作为 expected
                cur = conn.cursor()
                cur.execute(
                    "SELECT MAX(c) FROM ("
                    "  SELECT trade_date, COUNT(*) c FROM trade_stock_daily"
                    "  GROUP BY trade_date) x")
                mrow = cur.fetchone()
                cur.close()
                expected = int(mrow[0]) if mrow and mrow[0] else cnt
                pct = round(cnt / expected * 100, 1) if expected > 0 else 0.0
                # 完成度: 允许小幅正常波动(新股上市/ETF态/退市/暂停)导致的历史峰值差(<1%),
                # 与历史峰值持平或接近即视为已完整入库。
                out = {
                    "trade_date": date_s,
                    "count": cnt,
                    "expected": expected,
                    "pct": pct,
                    "complete": cnt >= expected or pct >= 99.0,
                }
        finally:
            conn.close()
    except Exception:
        out = {}
    _DAILY_INGEST_CACHE = {"ts": time.time(), "data": out}
    return out


def _prev_market_volume_by_date(day: str) -> dict:
    """某交易日所对应「昨日」全市场量能: trade_market_volume 中 trade_date < day 的最近一条.

    例如 day=2026-09-10 -> 返回 09-09 的量能 (若表中有 09-08/09-09 等更早日, 取 <=day 前最近一条有值的)。
    结果按 day 缓存避免每股重复查库。
    """
    if not day:
        return {}
    if day in _PMV_CACHE:
        return _PMV_CACHE[day]
    out = {}
    try:
        from lib.market_metrics import query_market_volume
        rows = query_market_volume()  # 倒序最近 60 条
        for r in rows:
            if r["trade_date"] < day:
                tot = r.get("total_amount") or 0
                if tot > 0:
                    out = {
                        "trade_date": r["trade_date"],
                        "total_amount": int(tot),
                        "text": f"{tot / 1e12:.2f} 万亿",
                    }
                break
    except Exception:
        pass
    _PMV_CACHE[day] = out
    return out


# 选股池「当前涨幅」(cur_pct) 只在 13:00 / 15:00 两个时刻定时更新(盘中/收盘实时行情)。
# 非更新时间点页面显示为空: 非交易日, 或交易日早于当日 13:00 时置空。


def _cur_pct_gate(cur_pct):
    """按当前时间闸门: 否则返回 None (前端显示 '-')。

    交易日 13:00-15:00 显示 13:00 写入的盘中值; 15:00 后显示 15:00 写入的收盘值;
    13:00 前 / 非交易日为空。
    """
    import datetime as _dt, calendar as _cal
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    if now.weekday() in (_cal.SATURDAY, _cal.SUNDAY):
        return None
    hm = now.hour * 100 + now.minute
    if hm < 1300:          # 早于当日 13:00 -> 空
        return None
    if cur_pct is None or cur_pct == "":
        return None
    return cur_pct


def _cur_pct_show(cur_pct, trade_date):
    """选股池「收盘涨幅」展示: 以入选当日收盘涨幅为准。

    - 历史批次(入选日 < 今天): 已收盘, 直接显示数据库里的收盘涨幅 cur_pct (不受当前时分门控);
    - 今日批次(入选日 == 今天): 走 _cur_pct_gate 时间门控 (13:00 前空, 13:00-15:00 盘中, 收盘后收盘涨幅)。
    """
    import datetime as _dt
    day = str(trade_date or "")[:10]
    if day and day < _dt.date.today().strftime("%Y-%m-%d"):
        if cur_pct is None or cur_pct == "":
            return None
        return cur_pct
    return _cur_pct_gate(cur_pct)


@router.get("/list")
def pool_list():
    """选股池: 按策略分组, 展示每个策略自己选出的候选股.

    当前涨幅(cur_pct) 全部来自数据库: 盘中不刷新(保持选股落库值), 休市后由调度任务
    backfill_selection_cur_pct 一次性补齐为最新日线收盘涨幅。页面刷新只读 DB, 快速加载。
    """
    sel_cfg = load_selection_config()
    instances = {s.get("name"): s for s in sel_cfg.get("strategies", [])}
    state = _load_state()

    price_map: dict = {}
    for p in state.get("positions", []):
        if p.get("code") and p.get("cur_price") is not None:
            price_map[p["code"]] = p["cur_price"]

    selection = get_selection_state()
    sel_per_strategy = selection.get("per_strategy", {}) or {}

    # 权威名称映射: 全表 5000 只稳定, 缓存避免每请求重查
    name_map = _name_map_cached()

    groups = []
    total_selected = 0
    for name, inst in instances.items():
        meta = get_strategy(name)
        label = inst.get("label") or (meta.label if meta else name)
        pool = query_selection_pool(strategy=name) or []
        # 若数据库为空, 回退到本次 selection 结果
        if not pool:
            pool = []
            for r in sel_per_strategy.get(name, []) or []:
                pool.append({
                    "stock_code": r.get("stock_code") or r.get("code"),
                    "name": r.get("name", ""),
                    "rank_no": r.get("rank_no"),
                    "score": r.get("score"),
                    "reason": r.get("reason", ""),
                    "trade_date": r.get("trade_date", ""),
                    "open_pct": r.get("open_pct"),
                    "cur_pct": r.get("cur_pct"),
                })
        rows = []
        for r in pool:
            code = str(r["stock_code"])
            rows.append({
                "code": code,
                "name": name_map.get(code) or (r.get("name", "") or ""),
                "rank": r.get("rank_no"),
                "score": r.get("score"),
                "reason": r.get("reason", ""),
                "trade_date": r.get("trade_date", ""),
                "selected_at": r.get("selected_at", ""),
                "open_pct": r.get("open_pct"),
                "cur_pct": _cur_pct_show(r.get("cur_pct"), r.get("trade_date")),
                "cur_price": price_map.get(code),
                # 市场量能 = 该股入选日(selected_at/trade_date)对应「昨日」的全市场量能
                "market_volume": _prev_market_volume_by_date(
                    (r.get("selected_at") or r.get("trade_date") or "")[:10]),
                "source": "策略选股",
                "side_text": "候选",
            })
        total_selected += len(rows)
        groups.append({
            "strategy": name,
            "label": label,
            "enabled": bool(inst.get("enabled")),
            "size": len(rows),
            "rows": rows,
        })

    return {
        "groups": groups,
        "total_selected": total_selected,
        "summary": {"strategies": len(instances), "selected": total_selected},
        "selection_run_at": selection.get("last_run_at", ""),
        "selection_trigger": selection.get("last_trigger", ""),
        "trade_date": selection.get("trade_date", ""),
        "market_volume": _prev_market_volume(),
        "daily_ingest": _latest_daily_ingest(),
        "updated_at": state.get("_updated_at", ""),
    }


@router.get("/config")
def pool_config_get():
    """读取选股池面板配置 + 09:28 推送分支逻辑状态。

    返回:
        min_market_yi      昨日量能阈值(万亿)
        prev_drop_min_pct  昨日阴线涨幅下限(%)
        push_logic         推送判定状态 {prev_trade_date, prev_amount_yi, threshold_yi,
                                         branch, amount_text, text, tip}
    """
    from lib.pool_config import load_min_market_yi, load_prev_drop_min_pct
    yi = load_min_market_yi()
    prev_drop = load_prev_drop_min_pct()
    mv = _prev_market_volume()
    total = mv.get("total_amount") if mv else None
    prev_amount_yi = (round(total / 1e12, 2) if isinstance(total, (int, float)) and total > 0 else None)
    if prev_amount_yi is None:
        branch, amount_text = None, "无量能数据"
    else:
        amount_text = f"{prev_amount_yi:.2f} 万亿"
        branch = "branch1" if prev_amount_yi > yi else "branch2"
    # 说明文本
    b1 = f"昨日量能 {amount_text} 高于阈值 {yi} 万亿 → 走分支一：推送【当日】选股候选"
    b2 = (f"昨日量能 {amount_text} 不高于阈值 {yi} 万亿 → 走分支二：推送【昨日】阴线候选"
          f"(收盘<开盘, 且昨日涨幅> {prev_drop}%)")
    text = b1 if branch == "branch1" else (b2 if branch == "branch2" else amount_text)
    push_logic = {
        "prev_trade_date": mv.get("trade_date") if mv else None,
        "prev_amount_yi": prev_amount_yi,
        "threshold_yi": yi,
        "branch": branch,
        "amount_text": amount_text,
        "text": text,
        "tip": b1 + " || " + b2,
    }
    return {"min_market_yi": yi,
            "min_market_yi_amount": yi * 1e12,
            "prev_drop_min_pct": prev_drop,
            "push_logic": push_logic}


@router.post("/config")
def pool_config_save(payload: Optional[Dict[str, Any]] = Body(None)):
    """保存选股池面板配置 (昨日量能阈值 万亿 + 昨日阴线涨幅下限 %)"""
    from lib.pool_config import (save_min_market_yi, save_prev_drop_min_pct,
                                  load_min_market_yi, load_prev_drop_min_pct)
    p = payload or {}
    msg = []
    if "min_market_yi" in p:
        try:
            yi = float(p.get("min_market_yi"))
        except Exception:
            return {"ok": False, "message": "请输入有效的量能阈值 (万亿)"}
        yi = save_min_market_yi(yi)
        msg.append(f"量能阈值 {yi} 万亿")
    if "prev_drop_min_pct" in p:
        try:
            prev_drop = float(p.get("prev_drop_min_pct"))
        except Exception:
            return {"ok": False, "message": "请输入有效的涨幅下限 (%)"}
        prev_drop = save_prev_drop_min_pct(prev_drop)
        msg.append(f"阴线涨幅下限 {prev_drop}%")
    if not msg:
        return {"ok": True, "message": "无变更"}
    return {"ok": True, "min_market_yi": load_min_market_yi(),
            "prev_drop_min_pct": load_prev_drop_min_pct(),
            "message": "已保存: " + "; ".join(msg)}