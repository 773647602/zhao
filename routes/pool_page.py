# -*- coding: utf-8 -*-
# 选股池路由 -- REST
"""
GET  /api/pool/list  -- 返回当前选股池, 按策略分组 (每个策略自己选出的候选股)
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE
setup_sys_path()

from lib.strategy_runner import (
    load_selection_config, get_selection_state,
)
from lib.selection_store import query_selection_pool
from lib.strategy_registry import get_strategy

router = APIRouter()


def _load_state() -> dict:
    if not OUTPUTS_LIVE_STATE.exists():
        return {}
    try:
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@router.get("/list")
def pool_list():
    """选股池: 按策略分组, 展示每个策略自己选出的候选股 (无 default / per_stock)"""
    sel_cfg = load_selection_config()
    instances = {s.get("name"): s for s in sel_cfg.get("strategies", [])}
    state = _load_state()

    # 现价映射 (来自 state.positions)
    price_map: dict = {}
    for p in state.get("positions", []):
        if p.get("code") and p.get("cur_price") is not None:
            price_map[p["code"]] = p["cur_price"]

    selection = get_selection_state()
    sel_per_strategy = selection.get("per_strategy", {}) or {}

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
            code = r["stock_code"]
            rows.append({
                "code": code,
                "name": r.get("name", "") or "",
                "rank": r.get("rank_no"),
                "score": r.get("score"),
                "reason": r.get("reason", ""),
                "trade_date": r.get("trade_date", ""),
                "selected_at": r.get("selected_at", ""),
                "open_pct": r.get("open_pct"),
                "cur_pct": r.get("cur_pct"),
                "source": "策略选股",
                "side_text": "候选",
                "cur_price": price_map.get(code),
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
        "updated_at": state.get("_updated_at", ""),
    }