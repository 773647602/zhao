# -*- coding: utf-8 -*-
# 多策略并行选股执行器 -- 每个策略在全市场上按各自条件选股, 写入选股池(库+json)
"""
run_all_selection(trigger):
    1. 读 config/strategy_selection.yaml -> 启用策略列表 (无 default / per_stock 概念)
    2. 一次性构建全市场快照 (批量 SQL, 5218 只)
    3. 对每个启用策略: 按各自选股条件(全市场过滤/top_n/selector)选出候选
    4. 把"每策略选股池"写入数据库 trade_selection_pool (幂等) + outputs/live_state.json
    5. 选股池只记录候选, 不生成买卖信号

数据源不可用时 (MySQL 无日K 或未连), 每策略返回空池, reason 标注原因。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.paths import OUTPUTS_LIVE_STATE, setup_sys_path

setup_sys_path()

from lib.live_simulator import load_mock_config
from lib.selection_engine import (
    build_universe_snapshot, run_strategy_selection, clear_snapshot_cache,
)
from lib.selection_store import (
    ensure_tables, replace_selection_pool_daily, sync_pool_to_watch_pool,
)

# 选股配置文件
SELECTION_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "strategy_selection.yaml"


# ============================================================
# 配置读取
# ============================================================

def load_selection_config() -> dict:
    """读 config/strategy_selection.yaml -> {universe: {...}, strategies: [...]}"""
    if not SELECTION_CONFIG_FILE.exists():
        return {"universe": {"source": "mysql"}, "strategies": []}
    try:
        import yaml
        cfg = yaml.safe_load(SELECTION_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        cfg.setdefault("universe", {"source": "mysql"})
        cfg["strategies"] = cfg.get("strategies", []) or []
        return cfg
    except Exception as e:
        print(f"[WARN] 读 strategy_selection.yaml 失败: {e}", flush=True)
        return {"universe": {"source": "mysql"}, "strategies": []}


def enabled_strategy_instances() -> List[dict]:
    """返回已启用的策略实例"""
    cfg = load_selection_config()
    return [s for s in cfg.get("strategies", []) if s.get("enabled")]


def parse_schedule(val: Any) -> List[str]:
    """规范化策略选股时刻 -> sorted 去重 ["HH:MM", ...]
    支持单值或列表; 中文逗号/半角逗号/空格分隔; 非法项跳过; None/空 -> [].
    """
    if val is None:
        return []
    items = val if isinstance(val, list) else [val]
    out: set = set()
    for it in items:
        if it is None:
            continue
        for part in str(it).replace("，", ",").replace("；", ";").replace(";", ",").split(","):
            part = part.strip()
            if ":" not in part:
                continue
            hh, _, mm = part.partition(":")
            if not (hh.strip().isdigit() and mm.strip().isdigit()):
                continue
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.add(f"{h:02d}:{m:02d}")
    return sorted(out)


def _normalize_selected_at(val: Optional[str]) -> Optional[str]:
    """规范化入选时间 -> "YYYY-MM-DD HH:MM:SS" (容错 datetime-local 的 T 与缺秒) 或 None
    """
    if not val:
        return None
    s = str(val).strip().replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def save_selection_config(cfg: dict) -> None:
    """写 config/strategy_selection.yaml"""
    import yaml
    try:
        SELECTION_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 多策略并行选股配置 (strategy -> stock-list 模型, 无默认策略)\n"
            "# 每个条目是一个可独立开关的策略实例, 各自配置在全市场(A股, 有日K)上按自己的选股条件选股。\n"
            "# 选股池只记录候选; 买入/卖出由各策略在各自候选+持仓上独立判断, 共享资金/独立持仓。\n"
            "# 修改后调用 POST /api/strategy/run 或等待定时选股即可生效。\n\n"
        )
        body = yaml.safe_dump(
            {"universe": cfg.get("universe", {"source": "mysql"}),
             "strategies": cfg.get("strategies", []) or []},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        SELECTION_CONFIG_FILE.write_text(header + body, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入 {SELECTION_CONFIG_FILE}: {e}") from e


# ============================================================
# 状态读写 (live_state.json 兼容)
# ============================================================

def _read_state() -> dict:
    if not OUTPUTS_LIVE_STATE.exists():
        return {}
    try:
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_selection_state() -> dict:
    """读 selection 结果 (供选股池 / 策略页展示)"""
    return _read_state().get("selection", {}) or {}


# ============================================================
# 主入口
# ============================================================

def run_all_selection(trigger: str = "manual", selected_at: Optional[str] = None) -> dict:
    """对每个启用策略在全市场按各自条件选股, 写入选股池(库+json)。
    selected_at: 入选时间 (立即选股时用户选取的时间), 缺省=当前时刻。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ensure_tables()

    sel_time = _normalize_selected_at(selected_at)  # "YYYY-MM-DD HH:MM:SS" 或 None(用当前)
    instances = enabled_strategy_instances()
    if not instances:
        return {"ok": False, "message": "未启用任何策略 (config/strategy_selection.yaml 为空)",
                "last_run_at": now, "per_strategy": {}, "by_strategy": {},
                "summary": {"total": 0, "selected": 0}, "trigger": trigger}

    clear_snapshot_cache()  # 每次重跑强制拉最新
    # 历史回放: 以"选取时间当日"为今日 (截断日线面板到该日); 无/非法则用最新交易日
    asof = (sel_time or "")[:10]
    snapshot = build_universe_snapshot(asof_date=asof or None)
    capital = float(load_mock_config().get("capital", 1_000_000))
    trade_date = getattr(snapshot, "latest_date", "") or now[:10]

    per_strategy: Dict[str, list] = {}
    reasons: Dict[str, str] = {}
    by_strategy: Dict[str, dict] = {}
    for inst in instances:
        name = inst.get("name", "")
        label = inst.get("label", name)
        try:
            rows = run_strategy_selection(inst, snapshot, capital)
            per_strategy[name] = rows
            reasons[name] = ""
        except Exception as e:
            per_strategy[name] = []
            reasons[name] = f"{type(e).__name__}: {e}"

        tot = len(per_strategy[name])
        by_strategy[name] = {
            "last_run_at": now, "total": tot, "selected": tot,
            "codes": [r["stock_code"] for r in per_strategy[name]],
            "label": label, "reason": reasons[name], "trade_date": trade_date,
        }

    # 写库 (每策略选股池, 幂等)
    try:
        written = replace_selection_pool_daily(per_strategy, trade_date, selected_at=sel_time, trigger=trigger)
    except Exception as e:
        written = 0
        reasons["_db"] = f"选股池写库失败: {e}"

    # 自动(定时)选股: 选出的候选并入监控池 watch_pool.yaml; 手动选股(trigger=manual)不入池
    if trigger != "manual":
        try:
            all_codes = list({r["stock_code"] for lst in per_strategy.values()
                              for r in lst if isinstance(r, dict)})
            if all_codes:
                added = sync_pool_to_watch_pool(all_codes)
                if added:
                    print(f"[selection] 已并入监控池 {len(added)} 只 (新增)", flush=True)
        except Exception as e:
            reasons["_watch_pool"] = f"监控池同步失败: {e}"

    # 写 live_state.json (selection 字段)
    selected_total = sum(len(v) for v in per_strategy.values())
    try:
        s = _read_state()
        s["selection"] = {
            "last_run_at": now, "last_trigger": trigger, "trade_date": trade_date,
            "selected_at": sel_time or now,
            "per_strategy": per_strategy, "by_strategy": by_strategy,
        }
        OUTPUTS_LIVE_STATE.write_text(
            json.dumps(s, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        write_ok = True
    except Exception as e:
        write_ok = False
        reasons["_json"] = f"结果写盘失败: {e}"

    msg = (f"选股完成: {len(instances)} 个策略, 共选出 {selected_total} 只候选"
           + (f", 写入数据库 {written} 行" if written else ""))
    return {
        "ok": True, "message": msg, "last_run_at": now, "trigger": trigger,
        "trade_date": trade_date,
        "summary": {"total": len(instances), "selected": selected_total,
                    "by_strategy": {k: v["total"] for k, v in by_strategy.items()}},
        "per_strategy": {k: v for k, v in per_strategy.items()},
        "by_strategy": by_strategy,
    }


def run_one_selection(name: str, trigger: str = "cron") -> dict:
    """仅对单个指定(且启用)策略跑一次选股, 写入选股池(库+json)。
    供'每策略各自选股时间'的定时调度逐策略触发; 不干扰其它策略的既有结果。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ensure_tables()

    cfg = load_selection_config()
    inst = next((s for s in cfg.get("strategies", [])
                 if s.get("name") == name and s.get("enabled")), None)
    if inst is None:
        return {"ok": False, "message": f"策略 {name} 不存在或未启用, 跳过",
                "name": name, "last_run_at": now}

    try:
        clear_snapshot_cache()
        snapshot = build_universe_snapshot(int(inst.get("lookback_days") or 120))
    except Exception as e:
        return {"ok": False, "message": f"快照构建失败: {e}", "name": name, "last_run_at": now}

    capital = float(load_mock_config().get("capital", 1_000_000))
    trade_date = getattr(snapshot, "latest_date", "") or now[:10]

    try:
        rows = run_strategy_selection(inst, snapshot, capital)
        reason = ""
    except Exception as e:
        rows = []
        reason = f"{type(e).__name__}: {e}"

    per_strategy = {name: rows}
    try:
        written = replace_selection_pool_daily(per_strategy, trade_date, trigger=trigger)
    except Exception as e:
        written = 0
        reason = reason or f"选股池写库失败: {e}"

    # 自动(定时)选股: 选出的候选并入监控池 watch_pool.yaml; 手动选股(trigger=manual)不入池
    if trigger != "manual":
        try:
            codes = [r["stock_code"] for r in rows if isinstance(r, dict)]
            if codes:
                added = sync_pool_to_watch_pool(codes)
                if added:
                    print(f"[selection] 策略 {name}: 已并入监控池 {len(added)} 只 (新增)",
                          flush=True)
        except Exception as e:
            reason = reason or f"监控池同步失败: {e}"

    by_strategy = {name: {
        "last_run_at": now, "total": len(rows), "selected": len(rows),
        "codes": [r["stock_code"] for r in rows],
        "label": inst.get("label", name), "reason": reason, "trade_date": trade_date,
    }}

    # 更新 live_state.json selection: 仅合并该策略的选股结果, 保留其它策略
    try:
        s = _read_state()
        sel = s.get("selection", {}) or {}
        per = dict(sel.get("per_strategy") or {})
        per[name] = rows
        bs = dict(sel.get("by_strategy") or {})
        bs.update(by_strategy)
        sel["per_strategy"] = per
        sel["by_strategy"] = bs
        sel["last_run_at"] = now
        sel["last_trigger"] = trigger
        sel["trade_date"] = trade_date
        s["selection"] = sel
        OUTPUTS_LIVE_STATE.write_text(
            json.dumps(s, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        write_ok = True
    except Exception as e:
        write_ok = False
        reason = reason or f"结果写盘失败: {e}"

    msg = (f"策略 {name}: 选出 {len(rows)} 只候选"
           + (f", 写入数据库 {written} 行" if written else ""))
    return {"ok": True, "name": name, "message": msg, "last_run_at": now,
            "trigger": trigger, "trade_date": trade_date,
            "selected": len(rows), "by_strategy": by_strategy}