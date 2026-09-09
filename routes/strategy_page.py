# -*- coding: utf-8 -*-
# 策略配置页路由 -- REST
"""
GET  /api/strategy/list      -- 所有注册策略 + 启用状态(来自 strategy_selection.yaml) + 分组 + 参数
GET  /api/strategy/config    -- 当前多策略选股配置 (无 default / per_stock)
GET  /api/strategy/params    -- 单个策略的参数清单 (含缺省值 + 当前生效值)
POST /api/strategy/params    -- 保存策略参数覆盖值 (即时生效, 无需重启)
GET  /api/strategy/selection -- 最近一次选股结果 (各策略执行时间 + 每策略候选)
POST /api/strategy/run       -- 立即执行一次多策略选股 (各策略按自身配置的缺省执行时间之外可手动触发)
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from lib.strategy_registry import (
    list_strategies, list_groups, get_strategy,
    get_params, set_params,
)
from lib.strategy_runner import (
    load_selection_config, enabled_strategy_instances,
    run_all_selection, run_one_selection, get_selection_state,
    save_selection_config, parse_schedule,
)
from lib.selection_store import query_selection_pool

router = APIRouter()


@router.post("/schedule")
def strategy_schedule_set(payload: Dict[str, Any] = Body(default={})):
    """配置单个策略的选股时刻 (HH:MM, 多个用逗号/空格分隔, 空=仅手动触发)。
    保存后写回 strategy_selection.yaml 实例的 schedule 字段, 供定时调度按各自时间触发.
    payload: {"name": "weak_to_strong", "schedule": "09:25,14:35"} (schedule 可为字符串或列表)
    """
    name = (payload or {}).get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    times = parse_schedule((payload or {}).get("schedule"))
    cfg = load_selection_config()
    insts = cfg.get("strategies", [])
    inst = next((s for s in insts if s.get("name") == name), None)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"策略 {name} 不存在")
    if times:
        inst["schedule"] = times
    else:
        inst.pop("schedule", None)  # 空 -> 仅手动触发
    save_selection_config(cfg)
    return {"ok": True, "name": name, "schedule": times,
            "message": "选股时间已保存" + (f": {', '.join(times)}" if times else " (仅手动触发)")}


@router.get("/weaktest/config")
def weaktest_config():
    """弱转强测试: 当前 weak_to_strong 实例的筛选阈值 + 买卖条件配置"""
    cfg = load_selection_config()
    inst = next((s for s in cfg.get("strategies", []) if s.get("name") == "weak_to_strong"), None)
    filters = ((inst or {}).get("selection") or {}).get("filters", {}) or {} if inst else {}
    trade = (inst or {}).get("trade", {}) or {}
    return {
        "ok": True,
        "label": (inst or {}).get("label", "弱转强"),
        "enabled": bool(inst and inst.get("enabled")),
        "filters": filters,
        "trade": trade,
    }


_WEAK_FILTER_KEYS = ("max_prev_pct", "min_open_pct", "min_vol_grow_pct",
                     "ma_period", "max_index_pct")

_TRADE_BUY_KEYS = ("open_pct_min", "open_pct_max", "mode", "dip_pct", "recover_pct",
                   "plunge_pct", "rebound_pct", "no_buy_up_pct")
_TRADE_SELL_KEYS = ("take_profit_pct", "stop_loss_pct")


def _merge_weak_trade(old: dict, given: dict) -> dict:
    """合并买卖条件配置: 前端未传的键保留 yaml 原值 (与 filters 漏传保护同思路)."""
    old = dict(old or {})
    given = dict(given or {})
    if "enabled" in given:
        old["enabled"] = bool(given.get("enabled"))
    else:
        old.setdefault("enabled", False)
    buy = dict(old.get("buy") or {})
    gbuy = dict(given.get("buy") or {})
    for k in _TRADE_BUY_KEYS:
        buy[k] = gbuy[k] if k in gbuy else buy.get(k)
    old["buy"] = buy
    sell = dict(old.get("sell") or {})
    gsell = dict(given.get("sell") or {})
    for k in _TRADE_SELL_KEYS:
        sell[k] = gsell[k] if k in gsell else sell.get(k)
    old["sell"] = sell
    return old


@router.post("/weaktest/config")
def weaktest_config_set(payload: Dict[str, Any] = Body(default={})):
    """保存弱转强筛选条件 + 买卖条件 -> strategy_selection.yaml
    payload: {"filters": {...}, "trade": {"enabled": true, "buy": {...}, "sell": {...}}}
    filters/trade 中 null=禁用该项目; 前端未传的键保留 yaml 原值
    """
    from lib.strategy_runner import save_selection_config
    given = (payload or {}).get("filters") or {}
    cfg = load_selection_config()
    # 前端未传的键保留 yaml 原值, 避免漏传导致参数被清空 (null=明确禁用)
    old = dict(((next((s for s in cfg.get("strategies", [])
                        if s.get("name") == "weak_to_strong"), None) or {}).get("selection") or {}).get("filters") or {})
    clean = {k: (given[k] if k in given else old.get(k)) for k in _WEAK_FILTER_KEYS}
    insts = cfg.get("strategies", [])
    inst = next((s for s in insts if s.get("name") == "weak_to_strong"), None)
    if inst is None:
        inst = {"name": "weak_to_strong", "label": "弱转强", "enabled": True,
                "universe": "all_a", "lookback_days": 20, "top_n": 20,
                "selection": {"mode": "generic_filter", "filters": {},
                              "sort_by": "amount", "sort_desc": True}}
        cfg["strategies"].append(inst)
    inst.setdefault("selection", {})["filters"] = clean
    inst["trade"] = _merge_weak_trade(inst.get("trade") or {},
                                      (payload or {}).get("trade") or {})
    try:
        save_selection_config(cfg)
    except Exception as e:
        return {"ok": False, "message": f"保存失败: {type(e).__name__}: {e}"}
    return {"ok": True, "message": "弱转强筛选与买卖条件已保存",
            "filters": clean, "trade": inst["trade"]}


@router.post("/weaktest/run")
def weaktest_run(payload: Dict[str, Any] = Body(default={})):
    """弱转强测试: 用给定阈值临时跑一次 (不写库), 返回候选清单
    payload: {"filters": {"max_open_prev": 10, ..., "max_index_open_pct": 0.5}}  null=禁用
    """
    import copy
    import pandas as _pd
    from lib.selection_engine import build_universe_snapshot, run_strategy_selection
    from lib.live_simulator import load_mock_config

    overrides = (payload or {}).get("filters") or {}
    cfg = load_selection_config()
    inst = next((s for s in cfg.get("strategies", []) if s.get("name") == "weak_to_strong"), None)
    if inst is None:
        inst = {"name": "weak_to_strong", "label": "弱转强", "enabled": True,
                "universe": "all_a", "lookback_days": 20, "top_n": 20,
                "selection": {"mode": "generic_filter", "filters": {},
                              "sort_by": "amount", "sort_desc": True}}
    inst = copy.deepcopy(inst)
    base = dict(((inst.get("selection") or {}).get("filters") or {}))
    filters = {}
    for k in set(list(base.keys()) + list(overrides.keys())):
        filters[k] = overrides.get(k, base.get(k))  # None -> 禁用该条件
    inst.setdefault("selection", {})["filters"] = filters
    top_n = int(inst.get("top_n") or 200)

    snap = build_universe_snapshot()
    rows = run_strategy_selection(inst, snap, float(load_mock_config().get("capital", 1_000_000)))
    meta = snap.meta

    def _num(v):
        try:
            f = float(v)
            return None if _pd.isna(f) else round(f, 3)
        except Exception:
            return None

    out = []
    for r in rows:
        code = r["stock_code"]
        rec = {"stock_code": code, "name": r.get("name", ""), "rank_no": r.get("rank_no")}
        if code in meta.index:
            for fld in ("close", "open", "prev_pct", "open_pct",
                        "vol_grow_pct", "above_ma5_prev", "above_ma10_prev",
                        "above_ma20_prev", "pct_change", "amount"):
                if fld in meta.columns:
                    rec[fld] = _num(meta.loc[code, fld])
                else:
                    rec[fld] = None
        out.append(rec)

    mper = filters.get("ma_period")
    return {
        "ok": True,
        "total": len(out),
        "latest_date": snap.latest_date,
        "index_pct": _num(snap.index_pct),
        "filters": filters,
        "ma_period": mper,
        "rows": out[: top_n],
    }


@router.get("/list")
def strategy_list():
    """返回所有注册策略 + 每个策略实例的启用状态 + 分组 + 选股池数量"""
    sel_cfg = load_selection_config()
    instances = {s.get("name"): s for s in sel_cfg.get("strategies", [])}
    groups = list_groups()
    flat = list_strategies()
    by_strategy = get_selection_state().get("by_strategy", {}) or {}
    flat_with_status = []
    for s in flat:
        s2 = dict(s)
        inst = instances.get(s["name"])
        s2["enabled"] = bool(inst and inst.get("enabled"))
        s2["configured"] = inst is not None
        b = by_strategy.get(s["name"], {})
        s2["last_run_at"] = b.get("last_run_at", "")
        s2["selected_count"] = b.get("selected", 0)
        flat_with_status.append(s2)
    sel = get_selection_state()
    return {
        "groups": groups,
        "flat": flat_with_status,
        "config": sel_cfg,
        "selection": {
            "last_run_at": sel.get("last_run_at", ""),
            "last_trigger": sel.get("last_trigger", ""),
            "trade_date": sel.get("trade_date", ""),
        },
    }


@router.get("/config")
def strategy_config():
    """当前多策略选股配置 (无 default / per_stock)"""
    return load_selection_config()


@router.post("/config")
def strategy_config_set(payload: Dict[str, Any] = Body(...)):
    """保存多策略选股配置 (config/strategy_selection.yaml)"""
    try:
        from lib.strategy_runner import save_selection_config
        if not isinstance(payload, dict) or "strategies" not in payload:
            return {"ok": False, "message": "payload 需为 {universe, strategies:[...]}"}
        save_selection_config(payload)
        return {"ok": True, "message": f"已保存 {len(payload.get('strategies', []))} 个策略实例",
                "config": load_selection_config()}
    except Exception as e:
        return {"ok": False, "message": f"保存失败: {type(e).__name__}: {e}"}


@router.get("/params")
def strategy_params_get(strategy: str):
    """单个策略的参数清单 (含缺省值 + 当前生效值), 供前端「配置」弹窗渲染"""
    if get_strategy(strategy) is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy} 未注册")
    return {"strategy": strategy, "params": get_params(strategy)}


@router.post("/params")
def strategy_params_set(payload: Dict[str, Any] = Body(...)):
    """保存策略参数覆盖值 -> config/strategy_params.json, 即时生效

    payload: {"strategy": "macd_1d", "values": {"fast": 12, "slow": 26, "signal": 9}}
    """
    name = payload.get("strategy")
    values = payload.get("values", {}) or {}
    if not name or get_strategy(name) is None:
        return {"ok": False, "message": f"策略 {name} 未注册"}
    if not isinstance(values, dict):
        return {"ok": False, "message": "values 必须是对象"}
    try:
        ignored = set_params(name, values)
    except Exception as e:
        return {"ok": False, "message": f"保存失败: {type(e).__name__}: {e}"}
    msg = f"参数已保存并生效: {name}"
    if ignored:
        msg += f" (忽略未知参数: {ignored})"
    return {"ok": True, "message": msg, "params": get_params(name)}


@router.get("/selection")
def strategy_selection():
    """最近一次选股结果 (各策略执行时间 + 每只股票信号)"""
    return get_selection_state()


@router.post("/run")
def strategy_run(payload: Dict[str, Any] = Body(default={})):
    """立即执行一次全策略选股 (各策略按自身配置的缺省执行时间之外可手动触发)。
    选出的当日符合条件个股进入选股池, 入选时间(selected_at)记录为用户当前选取的时间。
    payload: {"selected_at": "2026-09-07T09:30"} (可选; 缺省=当前时刻)
    """
    return run_all_selection(trigger="manual",
                             selected_at=(payload or {}).get("selected_at"))
