# -*- coding: utf-8 -*-
# 实盘监控路由 -- REST
"""
GET  /api/live/state                 -- 读 live_state.json
POST /api/live/control               -- 修改 control 字段 (pause_buying / force_clear_all 等)
POST /api/live/status                -- 修改 trading_status (PAUSED / RUNNING / HALTED)
GET  /api/live/strategies/registry   -- 列出所有可选策略 (按分组)
GET  /api/live/strategies/config     -- 读当前路由表 (default + per_stock)
POST /api/live/strategies/config     -- 保存路由表 (热加载)
GET  /api/live/watch_merge           -- 合并后的监控列表 (?ui= 额外逗号分隔代码)
GET  /api/live/watch_pool            -- 读监控代码列表 (watch_pool.yaml)
POST /api/live/watch_pool            -- 写监控代码列表
GET  /api/live/daily_watch           -- 当日选股池 + 是否已买入 + 名称映射
GET  /api/live/open_buy_window       -- 开盘买入窗口(09:29-09:35)任务进度
POST /api/live/stock/bind            -- 添加代码 + 绑定策略 (写 watch_pool + strategies.yaml 并热加载)
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE, OUTPUTS_DIR, PROJECT_ROOT
setup_sys_path()

import yaml as _yaml   # 读写 binding_source.yaml 用

from lib.live_simulator import (
    LiveSimRunner,
    load_strategy_config,
    load_mock_config,
    merge_watch_codes,
    load_watch_pool,
    save_watch_pool,
)
from lib.strategy_registry import list_groups, list_strategies
from lib.strategy_runner import load_selection_config

router = APIRouter()
_SIM = LiveSimRunner()


@router.get("/ping")
def live_ping():
    """健康检查：浏览器打开 /api/live/ping 可确认当前进程已加载 live 路由 (含 stock/bind)"""
    return {"ok": True, "module": "live", "hint": "绑定接口: POST /api/live/stock/bind"}


def _empty_state():
    return {
        "trading_status": "UNKNOWN", "capital": 0, "positions": [],
        "today_pnl": 0, "today_pnl_pct": 0,
        "events": [], "signals": [], "orders": [], "pnl_history": [],
        "control": {"pause_buying": False, "force_clear_all": False,
                    "max_daily_loss": -0.02, "dry_run": True},
        "health": {"miniqmt_connected": False, "last_heartbeat": None, "errors_24h": 0},
    }


def _load_state() -> dict:
    if not OUTPUTS_LIVE_STATE.exists():
        return _empty_state()
    try:
        return json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()


def _save_state(state: dict):
    state["_updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUTPUTS_LIVE_STATE)


# ------------------------------------------------------------
@router.get("/state")
def get_state():
    return _load_state()


def _append_event(state: dict, level: str, title: str, source: str = "ceo_console") -> None:
    """往 state.events 追加一条带级别的事件（告警四级分层，供前端徽章用）"""
    ev = {
        "ts":     datetime.now().isoformat(timespec="seconds"),
        "level":  level,
        "title":  title,
        "source": source,
    }
    state.setdefault("events", []).append(ev)
    state["events"] = state["events"][-200:]


# 危险操作的级别映射: control 的字段 / status 的取值 -> 事件级别
_CONTROL_LEVEL = {
    "force_clear_all": "CRITICAL",
    "pause_buying":    "WARN",
    "max_daily_loss":  "INFO",
    "dry_run":         "INFO",
}
_STATUS_LEVEL = {"HALTED": "CRITICAL", "PAUSED": "WARN", "RUNNING": "INFO"}


@router.post("/control")
def control(payload: dict = Body(...)):
    field = payload.get("field")
    value = payload.get("value")
    if not field:
        return {"ok": False, "message": "field 不能为空"}
    s = _load_state()
    s.setdefault("control", {})[field] = value
    level = _CONTROL_LEVEL.get(field, "INFO")
    _append_event(s, level, f"control.{field} = {value}")
    _save_state(s)
    return {"ok": True, "message": f"control.{field} = {value}"}


@router.post("/status")
def set_status(payload: dict = Body(...)):
    status = payload.get("status")
    if status not in ("RUNNING", "PAUSED", "HALTED"):
        return {"ok": False, "message": "status 必须是 RUNNING/PAUSED/HALTED"}
    s = _load_state()
    s["trading_status"] = status
    level = _STATUS_LEVEL.get(status, "INFO")
    _append_event(s, level, f"trading_status -> {status}")
    _save_state(s)
    return {"ok": True, "message": f"trading_status = {status}"}


# ============================================================
# 策略路由表 -- registry / config
# ============================================================

@router.get("/strategies/registry")
def strategies_registry():
    """列出所有可注册策略 (按分组)
    返回:
        {
          "groups": {"技术指标": [{"name", "label", "description"}, ...], ...},
          "flat":   [{"name", "label", "group", "description"}, ...]
        }
    """
    return {
        "groups": list_groups(),
        "flat":   list_strategies(),
    }


@router.get("/strategies/config")
def strategies_config_get():
    """读当前多策略选股配置 -- {universe, strategies:[...]} (无 default/per_stock)"""
    from lib.strategy_runner import load_selection_config
    return load_selection_config()


@router.post("/strategies/config")
def strategies_config_set(payload: dict = Body(...)):
    """保存多策略选股配置 + 热加载到 LiveSimRunner

    payload: {"universe": {...}, "strategies": [{name, enabled, universe, filters, top_n, ...}, ...]}
    """
    from lib.strategy_runner import load_selection_config
    if not isinstance(payload, dict) or "strategies" not in payload:
        return {"ok": False, "message": "payload 需为 {universe, strategies:[...]}"}

    # 校验所有引用的策略都已注册
    from lib.strategy_registry import list_strategies
    valid_names = {s["name"] for s in list_strategies()}
    invalid = [s.get("name") for s in payload.get("strategies", [])
               if s.get("name") not in valid_names]
    if invalid:
        return {"ok": False, "message": f"未知策略: {invalid}, 有效策略: {sorted(valid_names)}"}

    msg = _SIM.apply_strategy_config(payload)
    return {"ok": True, "message": msg, "config": load_selection_config()}


# ============================================================
# 监控列表合并 + 自选池
# ============================================================

# ============================================================
# 绑定来源标签 (用来区分: 这只股票的策略绑定是从「模拟盘」还是「实盘」加的)
#
# 设计动机:
#   实盘视图加股票时, 其实只是想让 AI 监控那只真实持仓股, 不希望污染模拟盘的「待入场」展示.
#   所以在 strategies.yaml 之外, 单独维护一个轻量 yaml 文件, 记录每个 code 的 source.
#   - sim  : 模拟盘里加的, 模拟盘「待入场」会显示, 引擎跑模拟成交
#   - real : 实盘里加的, 模拟盘「待入场」隐藏, 但引擎仍然算信号 (供「AI 信号 → 手动授权」用)
#   旧记录 / 缺省 source 一律视为 sim, 保持向后兼容.
# ============================================================

BINDING_SOURCE_FILE = PROJECT_ROOT / "config" / "binding_source.yaml"


def _load_binding_sources() -> Dict[str, str]:
    if not BINDING_SOURCE_FILE.exists():
        return {}
    try:
        data = _yaml.safe_load(BINDING_SOURCE_FILE.read_text(encoding="utf-8")) or {}
        sources = data.get("sources") or {}
        return {str(k): str(v) for k, v in sources.items() if v in ("sim", "real")}
    except Exception:
        return {}


def _save_binding_sources(sources: Dict[str, str]):
    BINDING_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = _yaml.safe_dump({"sources": sources}, allow_unicode=True, sort_keys=True)
    BINDING_SOURCE_FILE.write_text(text, encoding="utf-8")


def _set_binding_source(code: str, source: str):
    """更新单只股票的 source; source 必须是 'sim' / 'real'"""
    if source not in ("sim", "real"):
        return
    data = _load_binding_sources()
    data[code] = source
    _save_binding_sources(data)


def _drop_binding_source(code: str):
    data = _load_binding_sources()
    if code in data:
        data.pop(code, None)
        _save_binding_sources(data)


def _resolve_binding_sources(merged: list) -> Dict[str, str]:
    """给 mergedList 每个 code 推断 source:
       - binding_source.yaml 里显式记录过的 -> 用记录
       - 在 mock_positions / watch_pool / ui 里出现的 -> sim (模拟盘资源)
       - 都不在 -> real (推断是实盘绑的, 旧数据迁移用)
    """
    explicit = _load_binding_sources()
    mock_codes = {p.get("code") for p in load_mock_config().get("positions", []) if p.get("code")}
    watch_codes = set(load_watch_pool().get("codes") or [])
    sim_pool = mock_codes | watch_codes
    out: Dict[str, str] = {}
    for c in merged:
        if c in explicit:
            out[c] = explicit[c]
        elif c in sim_pool:
            out[c] = "sim"
        else:
            out[c] = "real"
    return out


@router.get("/watch_merge")
def watch_merge(ui: str = ""):
    """返回合并后的监控代码 (与启动模拟盘时一致)

    ui: 可选, 逗号分隔, 对应页面「额外监控」
    """
    ui_codes = [c.strip() for c in (ui or "").split(",") if c.strip()]
    merged = merge_watch_codes(ui_codes)
    return {
        "merged": merged,
        "binding_source": _resolve_binding_sources(merged),
        "sources": {
            "ui_extra":    ui_codes,
            "positions":   [p.get("code", "") for p in load_mock_config().get("positions", [])],
            "watch_pool":  load_watch_pool().get("codes", []),
            "strategy_keys": list(load_selection_config().get("strategies", [])),
        },
    }


@router.get("/watch_pool")
def watch_pool_get():
    return load_watch_pool()


@router.get("/daily_watch")
def daily_watch():
    """当日监控：今日各策略选出的候选股 + 是否已买入 (+ 全量代码→名称映射).

    数据源 trade_selection_pool(今日) + trade_strategy_position(实盘持仓, 判断已买入);
    names 覆盖 候选池∪持仓, 供「买入记录」「监控池」显示股票名称。
    """
    from datetime import date
    from lib.selection_store import query_selection_pool, query_strategy_positions
    from lib.strategy_registry import get_strategy
    from lib.selection_engine import _stock_name_map

    def label(n: str) -> str:
        try:
            meta = get_strategy(n)
            if meta is not None:
                return meta.label or n
        except Exception:
            pass
        return n

    today = date.today().isoformat()
    # 开盘买入窗口(09:29-09:35)最近落盘的判定详情: code -> {open_price, bought, buy_reason, not_buy_reason}
    try:
        from live_trading.open_buy_window import OpenBuyWindowRunner
        _win = OpenBuyWindowRunner.load_status() or {}
        _win_stocks = {str(x.get("code")): x for x in (_win.get("stocks") or [])}
    except Exception:
        _win_stocks = {}
    try:
        # 监控池只显示策略自动(trigger='cron')选出的候选; 手动选股(trigger='manual')不入监控池。
        # 取「最近一批」而非「今天」: 定时选股在 09:26 跑, 当日日线未入库,
        # 写库 trade_date=昨日, 按今天过滤会导致监控池为空。
        pool = query_selection_pool(trigger="cron") or []
        if pool:
            _latest = max(str(r["trade_date"]) for r in pool)
            pool = [r for r in pool if str(r["trade_date"]) == _latest]
    except Exception:
        pool = []
    try:
        positions = query_strategy_positions() or []
    except Exception:
        positions = []
    held = {str(p["stock_code"]) for p in positions
            if int(p.get("volume") or 0) > 0}
    # 并入内存实时持仓 (state.json): 模拟盘成交也标记「已买入」, 与「实时持仓」同源
    try:
        import json
        live_state = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
        for p in live_state.get("positions", []) or []:
            if int(p.get("volume") or 0) > 0:
                held.add(str(p.get("code", "")))
    except Exception:
        pass
    items = []
    need_names = set(held)
    for r in pool:
        code = str(r["stock_code"])
        need_names.add(code)
        wd = _win_stocks.get(code) or {}
        items.append({
            "code": code,
            "name": (r.get("name") or ""),
            "strategy": (r.get("strategy") or ""),
            "strategy_label": label((r.get("strategy") or "")),
            "selected_at": (r.get("selected_at") or r.get("trade_date") or ""),
            "open_pct": r.get("open_pct"),
            "bought": 1 if code in held else 0,
            # 开盘买入窗口的判定详情 (监控池展示)
            "open_price":     wd.get("open_price"),
            "buy_reason":     wd.get("buy_reason") or "",
            "not_buy_reason": wd.get("not_buy_reason") or "",
        })
    try:
        base = _stock_name_map()
    except Exception:
        base = {}
    names = {c: base.get(c, "") for c in need_names if base.get(c)}
    return {"items": items, "names": names, "monitor_date": today}


@router.get("/open_buy_window")
def open_buy_window_status():
    """开盘买入窗口(09:29-09:35)任务进度 -- 读 OpenBuyWindowRunner 落盘状态

    返回 (来自 outputs/live_open_buy_window.json):
      {
        "date", "running", "running_text", "active_count", "ordered_count",
        "window_end_count", "error_count", "cycle_seconds", "last_cycle_at",
        "last_error", "window_start", "window_end",
        "candidates": [...],        # 本轮候选代码
        "stocks": [...],            # 每只候选判定详情 (任务状态/开盘价/已买·未买原因)
        "orders": [...]             # 窗口内下单结果
      }
    """
    try:
        from live_trading.open_buy_window import OpenBuyWindowRunner
        data = OpenBuyWindowRunner.load_status() or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    # 兜底默认窗口配置 (未启动/文件不存在时前端也有展示数据)
    data.setdefault("date", "")
    data.setdefault("running", False)
    data.setdefault("running_text", "未启动")
    data.setdefault("active_count", 0)
    data.setdefault("ordered_count", 0)
    data.setdefault("window_end_count", 0)
    data.setdefault("error_count", 0)
    data.setdefault("cycle_seconds", 15)
    data.setdefault("last_cycle_at", None)
    data.setdefault("last_error", None)
    data.setdefault("window_start", "09:29")
    data.setdefault("window_end", "09:35")
    data.setdefault("candidates", [])
    data.setdefault("stocks", [])
    data.setdefault("orders", [])
    return data


def _normalize_stock_code(raw: str) -> str:
    """去掉空格并统一大写后缀 (.SH/.SZ/.BJ)"""
    s = (raw or "").strip().replace(" ", "").upper()
    return s


@router.post("/watch_pool")
def watch_pool_set(payload: Optional[Dict[str, Any]] = Body(None)):
    """写 watch_pool.yaml；失败时仍返回 JSON, 避免前端拿不到原因"""
    payload = payload or {}
    try:
        raw = payload.get("codes", "")
        if isinstance(raw, list):
            codes = [str(c).strip() for c in raw if str(c).strip()]
        else:
            codes = [
                c.strip()
                for c in str(raw or "").replace("，", ",").replace("\n", ",").split(",")
                if c.strip()
            ]
        codes = [_normalize_stock_code(c) for c in codes]
        save_watch_pool(codes)
        return {"ok": True, "message": f"[OK] 监控列表已保存, 共 {len(codes)} 只", "codes": codes}
    except Exception as e:
        return {"ok": False, "message": str(e), "codes": []}


# ============================================================
# 实盘账户 (miniQMT) -- 真实持仓 / 资金 / 委托
# ============================================================

# 进程级缓存: trader 实例 + 上次查询时间戳, 避免每 5 秒前端轮询都重连 miniQMT
_REAL_TRADER = None
_REAL_CACHE: Dict[str, Any] = {"asset": {}, "positions": [], "orders": [],
                                "ts": 0.0, "error": None}
_REAL_CACHE_TTL = 5.0   # 秒

# xtquant 委托状态码 -> 中文（与 xtconstant / 常见回报含义对齐）
_ORDER_STATUS_MAP = {
    48: "未知",   49: "未报",   50: "待报",   51: "已报",
    52: "已报待撤", 53: "部成待撤", 54: "部撤",   55: "已撤",
    56: "部成",   57: "已成",   58: "废单",
}
# 状态码 < 56 视为「未结束」(可撤单 / 等待成交)
_ORDER_PENDING_STATUS = {49, 50, 51, 52, 53}


def _query_real_orders_dict(trader) -> list:
    """直接走底层 _trader.query_stock_orders, 转 dict; V2 trader 没暴露这个方法, 在这里包一层"""
    try:
        raw = trader._trader.query_stock_orders(trader._account)
    except Exception:
        return []
    if not raw:
        return []
    out = []
    for o in raw:
        side_code = getattr(o, "order_type", 0)
        status_code = getattr(o, "order_status", 0)
        out.append({
            "order_id":      getattr(o, "order_id", 0),
            "stock_code":    getattr(o, "stock_code", ""),
            "side":          "buy" if side_code == 23 else ("sell" if side_code == 24 else f"type_{side_code}"),
            "order_volume":  getattr(o, "order_volume", 0),
            "traded_volume": getattr(o, "traded_volume", 0),
            "price":         float(getattr(o, "price", 0) or 0),
            "order_status":  status_code,
            "status_text":   _ORDER_STATUS_MAP.get(status_code, f"未知({status_code})"),
            "cancelable":    status_code in _ORDER_PENDING_STATUS,
            "order_time":    getattr(o, "order_time", 0),
            "strategy_name": getattr(o, "strategy_name", ""),
            "order_remark":  getattr(o, "order_remark", ""),
        })
    return out


def _get_real_trader():
    """懒加载 trader 实例; 失败时抛异常 (调用方负责捕获)"""
    global _REAL_TRADER
    if _REAL_TRADER is not None:
        return _REAL_TRADER

    qmt_path = os.environ.get("QMT_PATH", "").strip()
    account_id = os.environ.get("ACCOUNT_ID", "").strip()
    if not qmt_path or not account_id:
        raise RuntimeError("未配置 QMT_PATH / ACCOUNT_ID, 请在 .env 中设置后重启 app")

    setup_sys_path()  # live_trading / lib 等路径
    from miniqmt_trader_v2 import MiniQMTTraderV2  # type: ignore
    trader = MiniQMTTraderV2(
        qmt_path=qmt_path,
        account_id=account_id,
        enable_heartbeat=True,     # 实盘视图: 后台心跳保活, 探测断线
        heartbeat_interval=10,     # 10s 心跳
        enable_reconnect=True,     # 断线自动重连 bridge (指数退避)
        max_reconnect_attempts=8,  # 单轮最长约 4 分钟; 心跳每轮仍会持续重试
    )
    trader.connect()
    _REAL_TRADER = trader
    return trader


@router.get("/real_account")
def real_account():
    """实盘视图用: 拉 miniQMT 真实账户 + 持仓 + 当日委托 (5 秒进程级缓存).

    返回格式 (无论成功失败都返回 200, 由 connected 字段告诉前端):
      {
        "connected": true/false,
        "error":     null | "失败原因",
        "asset":     {"total_asset", "cash", "market_value", "frozen_cash"} | {},
        "positions": [{"stock_code","volume","can_use_volume","open_price","market_value"}],
        "orders":    [{"order_id","stock_code","side","order_volume","traded_volume",
                       "price","order_status","status_text","cancelable","order_time"}],
        "cached_age_sec": <float>,
      }
    """
    import time as _time
    account_id = os.environ.get("ACCOUNT_ID", "")
    now = _time.time()
    age = now - (_REAL_CACHE.get("ts") or 0)
    if _REAL_CACHE.get("ts") and age < _REAL_CACHE_TTL:
        return {
            "connected": _REAL_CACHE.get("error") is None,
            "error":     _REAL_CACHE.get("error"),
            "account_id": account_id,
            "asset":     _REAL_CACHE.get("asset") or {},
            "positions": _REAL_CACHE.get("positions") or [],
            "orders":    _REAL_CACHE.get("orders") or [],
            "cached_age_sec": round(age, 2),
        }

    try:
        trader = _get_real_trader()
        asset = trader.query_asset() or {}
        positions = trader.query_positions() or []
        orders = _query_real_orders_dict(trader)
        _REAL_CACHE.update({"asset": asset, "positions": positions,
                            "orders": orders, "ts": now, "error": None})
        return {
            "connected": True, "error": None,
            "account_id": account_id,
            "asset": asset, "positions": positions, "orders": orders,
            "cached_age_sec": 0.0,
        }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _REAL_CACHE.update({"asset": {}, "positions": [], "orders": [],
                            "ts": now, "error": err})
        return {
            "connected": False, "error": err,
            "account_id": account_id,
            "asset": {}, "positions": [], "orders": [],
            "cached_age_sec": 0.0,
        }


@router.post("/real_reconnect")
def real_reconnect():
    """强制断开并重连国金 miniQMT 交易账号 (资金面板「重连 QMT」按钮调用).

    流程: 断开旧 trader 单例 -> 置空 -> 重新连接 -> 立即拉一次真实账户/持仓。
    """
    import time as _time
    global _REAL_TRADER
    old = _REAL_TRADER
    _REAL_TRADER = None
    _REAL_CACHE["ts"] = 0.0   # 使进程级缓存立即过期
    if old is not None:
        try:
            old.disconnect()
        except Exception:
            pass
    try:
        trader = _get_real_trader()
        asset = trader.query_asset() or {}
        positions = trader.query_positions() or []
        orders = _query_real_orders_dict(trader)
        _REAL_CACHE.update({"asset": asset, "positions": positions,
                            "orders": orders, "ts": _time.time(), "error": None})
        return {"ok": True, "connected": True, "message": "QMT 已重连成功",
                "asset": asset, "positions": positions}
    except Exception as e:
        return {"ok": False, "connected": False,
                "message": f"重连失败: {type(e).__name__}: {e}"}


@router.post("/real_order/cancel")
def real_order_cancel(payload: Optional[Dict[str, Any]] = Body(None)):
    """撤销 miniQMT 上指定委托

    body: {"order_id": <int>}
    """
    payload = payload or {}
    raw_id = payload.get("order_id")
    try:
        order_id = int(raw_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"order_id 必须是整数, 收到: {raw_id!r}"}

    try:
        trader = _get_real_trader()
        result = trader.cancel(order_id)
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}

    # 撤单后立刻让缓存过期, 下一次 real_account 会重新拉
    _REAL_CACHE["ts"] = 0.0

    if result == 0:
        return {"ok": True, "message": f"已提交撤单请求: 编号 {order_id}"}
    return {"ok": False, "message": f"撤单失败: 编号 {order_id}, miniQMT 返回 {result}"}


# ============================================================
# AI 信号 → 手动授权下单 (实盘视图用)
#
# 设计:
#   - 引擎照常跑 (dry_run=True), 把 buy/sell 信号写到 state.signals.
#   - 实盘视图把这些信号当作 "AI 建议", 用户点 [授权] 才走 miniQMT 真实下单.
#   - 授权 / 拒绝 / 过期状态写到独立文件 OUTPUTS_DIR/live_approvals.json,
#     不覆盖主 live_state.json。
#   - 5 分钟未处理自动过期 (避免旧信号误下).
#   - 单笔金额按"总资金 * 10% / 现价 / 100 * 100" 算, 与 live_loop.py 一致.
# ============================================================

_APPROVALS_FILE = OUTPUTS_DIR / "live_approvals.json"
_APPROVAL_TTL_SEC = 300   # 5 分钟


def _signal_id(sig: dict) -> str:
    """根据信号生成稳定 id (ts + code + side); 同一信号在状态文件里唯一"""
    return f"{sig.get('ts', '')}|{sig.get('code', '')}|{sig.get('side', '')}"


def _load_approvals() -> Dict[str, Any]:
    if not _APPROVALS_FILE.exists():
        return {}
    try:
        return json.loads(_APPROVALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_approvals(data: Dict[str, Any]):
    _APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _APPROVALS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _APPROVALS_FILE)


def _calc_suggested_quantity(price: float, capital: float) -> int:
    """与 live_loop.py 风控一致: 单笔 <= 总资金 10%, 100 股一手, 不足按 100 试探"""
    if price <= 0:
        return 100
    max_amount = (capital or 0) * 0.10
    qty = int(max_amount / price / 100) * 100
    return qty if qty > 0 else 100


def _signal_age_sec(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts)
    except Exception:
        return 0.0
    return (datetime.now() - t).total_seconds()


@router.get("/approvals")
def approvals_list():
    """实盘视图调用: 把最近 buy/sell 信号 + approval 状态 + 风控建议数量 一起返回.

    返回:
      {
        "items": [
          {"id", "ts", "code", "side", "strategy", "reason",
           "suggested_quantity", "suggested_price",
           "status": "pending"|"approved"|"rejected"|"expired",
           "processed_ts": <ISO>|null,
           "order": {...}|null,
           "error": null|"...",
           "age_sec": <float>}
        ],
        "ttl_sec": 300,
        "capital": <float>,
      }
    """
    state = _load_state()
    signals = state.get("signals") or []
    approvals = _load_approvals()
    capital = float(state.get("capital") or load_mock_config().get("capital") or 1_000_000)

    # 持仓最新价 -- 用于风控数量估算
    pos_price = {p.get("code"): float(p.get("cur_price") or 0)
                 for p in (state.get("positions") or [])}

    # === 实盘账户的 持仓 / 可用现金 (用 cache, 不强制刷新; UI 自己每 5 秒拉) ===
    real_positions_map = {}
    real_cash = 0.0
    if _REAL_CACHE.get("ts"):
        for p in (_REAL_CACHE.get("positions") or []):
            real_positions_map[p.get("stock_code")] = p
        real_cash = float((_REAL_CACHE.get("asset") or {}).get("cash") or 0)

    def _eligibility(side: str, code: str, qty: int, price_hint: float):
        """判断这条信号在实盘里是否可下单. 返回 (eligible: bool, reason: str)"""
        if not _REAL_CACHE.get("ts"):
            # miniQMT 还没拉过, 不阻断 (用户点了会走 approve 后端校验)
            return True, ""
        if side == "sell":
            pos = real_positions_map.get(code)
            if not pos:
                return False, f"实盘无 {code} 持仓, 无法卖"
            can_use = int(pos.get("can_use_volume") or 0)
            if can_use < qty:
                return False, f"可用 {can_use} < 建议 {qty} (T+1 冻结)"
        else:
            need = qty * (price_hint or 0)
            if price_hint > 0 and real_cash > 0 and need > real_cash:
                return False, f"现金 {real_cash:,.0f} < 需 {need:,.0f}"
        return True, ""

    items = []
    # 最近 30 条 buy/sell, 倒序 (最新在上)
    recent = [s for s in signals if s.get("side") in ("buy", "sell")][-30:]
    for sig in reversed(recent):
        sid = _signal_id(sig)
        rec = approvals.get(sid) or {}
        age = _signal_age_sec(sig.get("ts", ""))
        status = rec.get("status") or "pending"
        # 5 分钟未处理 -> 过期; 已处理(approved/rejected)保持原状态不会被覆盖
        if status == "pending" and age > _APPROVAL_TTL_SEC:
            status = "expired"
        price = pos_price.get(sig.get("code"), 0.0)
        qty = _calc_suggested_quantity(price, capital)
        eligible, eligible_reason = _eligibility(sig.get("side"), sig.get("code"), qty, price)
        items.append({
            "id":         sid,
            "ts":         sig.get("ts", ""),
            "code":       sig.get("code", ""),
            "side":       sig.get("side", ""),
            "strategy":   sig.get("strategy", ""),
            "reason":     sig.get("reason", ""),
            "suggested_quantity": qty,
            "suggested_price":    price,   # 0 时前端展示「市价」, 下单也走市价
            "status":     status,
            "processed_ts": rec.get("processed_ts"),
            "order":      rec.get("order"),
            "error":      rec.get("error"),
            "age_sec":    round(age, 1),
            "eligible":         eligible,
            "eligible_reason":  eligible_reason,
        })
    return {"items": items, "ttl_sec": _APPROVAL_TTL_SEC, "capital": capital}


def _find_signal_by_id(sid: str) -> Optional[dict]:
    state = _load_state()
    for sig in (state.get("signals") or []):
        if _signal_id(sig) == sid:
            return sig
    return None


@router.post("/approvals/approve")
def approvals_approve(payload: Optional[Dict[str, Any]] = Body(None)):
    """授权下单: 通过 miniQMT 真实下单, 状态写入 approvals.json.

    body: {"id": "<signal_id>", "quantity": <int 可选, 覆盖建议数量>, "price": <float 可选, 0=市价>}
    """
    payload = payload or {}
    sid = str(payload.get("id", "")).strip()
    if not sid:
        return {"ok": False, "message": "id 不能为空"}

    sig = _find_signal_by_id(sid)
    if sig is None:
        return {"ok": False, "message": f"找不到信号 (可能已过期被清理): {sid}"}

    approvals = _load_approvals()
    if approvals.get(sid, {}).get("status") in ("approved", "rejected"):
        return {"ok": False, "message": f"信号已处理过 (status={approvals[sid].get('status')})"}
    if _signal_age_sec(sig.get("ts", "")) > _APPROVAL_TTL_SEC:
        return {"ok": False, "message": f"信号已过期 (>{_APPROVAL_TTL_SEC}s), 请等待新信号"}

    code = sig.get("code", "")
    side = sig.get("side", "")
    if side not in ("buy", "sell"):
        return {"ok": False, "message": f"信号方向异常: {side}"}

    # 数量: 用户传了就用, 否则按风控算
    state = _load_state()
    capital = float(state.get("capital") or load_mock_config().get("capital") or 1_000_000)
    pos_price = {p.get("code"): float(p.get("cur_price") or 0)
                 for p in (state.get("positions") or [])}
    price_hint = pos_price.get(code, 0.0)
    quantity = int(payload.get("quantity") or _calc_suggested_quantity(price_hint, capital))
    price = float(payload.get("price") or 0)   # 0 -> 市价

    # === 实盘前置校验 (避免下了券商必拒的单) ===
    real_acc = _REAL_CACHE if _REAL_CACHE.get("ts") else None
    if side == "sell":
        # 必须实盘账户里有这只持仓且 can_use_volume >= quantity
        positions = (real_acc or {}).get("positions") or []
        match = next((p for p in positions if p.get("stock_code") == code), None)
        if not match:
            return {"ok": False, "message": f"实盘持仓里没有 {code}, 无法卖出 (T+1 限制)"}
        can_use = int(match.get("can_use_volume") or 0)
        if can_use < quantity:
            return {"ok": False,
                    "message": f"可用持仓不足: {code} 可用 {can_use} 股 < 建议 {quantity} 股 (其余被 T+1 冻结)"}
    elif side == "buy":
        # 现金校验 (粗略: 按 price 或 0 跳过)
        cash = float((real_acc or {}).get("asset", {}).get("cash") or 0)
        if cash > 0 and price_hint > 0 and quantity * price_hint > cash:
            return {"ok": False,
                    "message": f"可用现金不足: 需要 ~{quantity * price_hint:,.0f} 元, 实盘可用 {cash:,.0f} 元"}

    # 调 miniQMT 真实下单
    err = None
    order = None
    try:
        trader = _get_real_trader()
        if side == "buy":
            order_id = trader.buy(code, quantity, price=price,
                                  strategy_name=sig.get("strategy", ""),
                                  remark="manual_approval")
        else:
            order_id = trader.sell(code, quantity, price=price,
                                   strategy_name=sig.get("strategy", ""),
                                   remark="manual_approval")
        if order_id is None or order_id < 0:
            err = f"miniQMT 返回 order_id={order_id} (常见原因: 未连接 / 风控拦截 / 余额不足)"
        else:
            order = {"order_id": order_id, "code": code, "side": side,
                     "quantity": quantity, "price": price}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    approvals[sid] = {
        "status":       "approved" if err is None else "rejected",
        "processed_ts": datetime.now().isoformat(timespec="seconds"),
        "order":        order,
        "error":        err,
    }
    _save_approvals(approvals)

    if err:
        return {"ok": False, "message": f"下单失败: {err}", "error": err}
    return {"ok": True, "message": f"已授权下单 {side} {code} {quantity}股", "order": order}


@router.post("/approvals/reject")
def approvals_reject(payload: Optional[Dict[str, Any]] = Body(None)):
    """拒绝信号: 标记不下单, 状态落盘"""
    payload = payload or {}
    sid = str(payload.get("id", "")).strip()
    if not sid:
        return {"ok": False, "message": "id 不能为空"}

    approvals = _load_approvals()
    if approvals.get(sid, {}).get("status") in ("approved", "rejected"):
        return {"ok": False, "message": f"信号已处理过 (status={approvals[sid].get('status')})"}

    approvals[sid] = {
        "status":       "rejected",
        "processed_ts": datetime.now().isoformat(timespec="seconds"),
        "order":        None,
        "error":        None,
    }
    _save_approvals(approvals)
    return {"ok": True, "message": "已拒绝该信号"}


@router.post("/stock/bind")
@router.post("/stock/bind/")
def stock_bind(payload: Optional[Dict[str, Any]] = Body(None)):
    """添加一只股票到监控列表, 并写入 per_stock 策略绑定 (写盘 + 热加载)

    body: {"code": "002432.SZ", "strategy": "dual_ma_5min", "source": "sim"|"real"}
    source 默认 'sim'; 'real' 表示这只股票是从「实盘」视图绑的, 模拟盘的「待入场」不显示它.
    """
    payload = payload or {}
    code = _normalize_stock_code(str(payload.get("code", "")))
    strategy = str(payload.get("strategy", "")).strip()
    source = str(payload.get("source", "sim")).strip().lower()
    if source not in ("sim", "real"):
        source = "sim"
    if not code:
        return {"ok": False, "message": "请填写股票代码 (例: 002432.SZ)"}
    if not strategy:
        return {"ok": False, "message": "请选择策略"}

    valid_names = {s["name"] for s in list_strategies()}
    if strategy not in valid_names:
        return {"ok": False, "message": f"未知策略: {strategy}, 有效: {sorted(valid_names)}"}

    try:
        # source=real 时不写 watch_pool (watch_pool 是模拟盘的自选池, 别污染);
        # 但 strategies.per_stock 还是要写, 否则引擎不会算它的信号 (实盘授权用).
        if source == "sim":
            wp = load_watch_pool()
            codes = list(wp.get("codes") or [])
            if code not in codes:
                codes.append(code)
            save_watch_pool(codes)
        else:
            # source=real: 反向把 code 从 watch_pool 拉出来 (兼容旧数据 -- 之前的版本会写入 watch_pool)
            wp = load_watch_pool()
            codes = list(wp.get("codes") or [])
            if code in codes:
                codes = [c for c in codes if c != code]
                save_watch_pool(codes)

        cfg = load_strategy_config()
        per = dict(cfg.get("per_stock") or {})
        per[code] = strategy
        default = cfg.get("default", "macd_5min")
        if default not in valid_names:
            default = "macd_5min"

        msg = _SIM.apply_strategy_config(default=default, per_stock=per)
        _set_binding_source(code, source)
        return {
            "ok": True,
            "message": f"已添加 {code} ({source}), 策略: {strategy}。{msg}",
            "codes": codes,
            "per_stock": per,
            "default": default,
            "source": source,
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/stock/unbind")
@router.post("/stock/unbind/")
def stock_unbind(payload: Optional[Dict[str, Any]] = Body(None)):
    """解除某只股票的策略绑定, 并把它从 watch_pool 里移除.

    用途: 实盘里有真实持仓但不想让引擎对它产出 buy/sell 信号
          (典型场景: 自己手动操作, 不想被 AI 误下单).

    注意:
        - mock_positions.yaml 里的初始模拟持仓不会被动 (那是模拟盘演示用的种子数据).
        - 解绑后该 code 仍可能因为在 mock_positions 里残留在 mergedList,
          此时引擎会用 default 策略跑它; 如果连默认信号都不想要, 请同时改 mock_positions.yaml.

    body: {"code": "002432.SZ"}
    """
    payload = payload or {}
    code = _normalize_stock_code(str(payload.get("code", "")))
    if not code:
        return {"ok": False, "message": "请填写股票代码"}

    actions = []
    try:
        wp = load_watch_pool()
        codes = list(wp.get("codes") or [])
        if code in codes:
            codes = [c for c in codes if c != code]
            save_watch_pool(codes)
            actions.append(f"已从 watch_pool 移除")

        cfg = load_strategy_config()
        per = dict(cfg.get("per_stock") or {})
        if code in per:
            per.pop(code, None)
            actions.append(f"已解除策略绑定")
        default = cfg.get("default", "macd_5min")
        valid_names = {s["name"] for s in list_strategies()}
        if default not in valid_names:
            default = "macd_5min"
        msg = _SIM.apply_strategy_config(default=default, per_stock=per)
        _drop_binding_source(code)

        if not actions:
            return {"ok": True,
                    "message": f"{code} 本来就没绑策略 / 不在 watch_pool, 无需操作",
                    "codes": codes, "per_stock": per}

        return {
            "ok": True,
            "message": f"{code} {' + '.join(actions)}; 引擎已热加载, 不会再对该股出信号. {msg}",
            "codes": codes,
            "per_stock": per,
            "default": default,
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}
