# -*- coding: utf-8 -*-
# 龙头战法路由 -- 候选名单 + 一键加入监控池
"""
GET  /api/dragon/candidates    -- 拉今日龙头候选
                                  数据源优先级 (默认 source=auto):
                                    1) MySQL wucai_trade.* 最近一个真实交易日 (含板块共振 v2 全开)
                                    2) xtdata 实时盘中行情 (盘中无板块, 自动关共振)
                                    3) mock 教学样本 (前两个都不可用时的兜底)
                                  query 参数:
                                    source=auto|mysql|xtdata|mock   数据源选择, 默认 auto
                                    use_xtdata=1                    旧参数, 等价 source=xtdata
                                    min_change=0.05                 最低涨幅
                                    max_price=30                    最高价格
                                    min_vol_ratio=2.0               最小量比
                                    require_resonance=-1            v2 板块共振:
                                                                    1=开 0=关 -1=按 source 自动
                                                                    (mysql/mock 默认开; xtdata 默认关)

POST /api/dragon/bind          -- 一键把候选写入 watch_pool + strategies.per_stock=dragon_picker
                                  body: {"codes": ["300750.SZ", "688981.SH"]}
                                  绑定后, 主循环下一轮就用 dragon_picker 策略给这些股票自动算
                                  入场 (涨幅>5% + 量比>2 + 价位<30 + 龙头分>=1.5)
                                  和出场 (当日跌幅>3% 或 从当日最高回撤>3%) 信号.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query

from lib.paths import setup_sys_path
setup_sys_path()

from lib.live_simulator import (
    LiveSimRunner, load_strategy_config, load_watch_pool, save_watch_pool,
)
from dragon_strategy.dragon_picker import (
    filter_dragon_candidates, _build_mock_today_stocks,
)
from dragon_strategy.dragon_backtest import (
    load_status_meta,
    load_sector_panel,
    load_daily_panel,
    build_today_candidates,
)


router = APIRouter()
_SIM = LiveSimRunner()

DRAGON_STRATEGY = "dragon_picker"


def _ensure_market_dot(code: str) -> str:
    """补全市场后缀 (002432 -> 002432.SZ; 600519 -> 600519.SH)"""
    code = code.strip().upper()
    if "." in code:
        return code
    if not code.isdigit() or len(code) != 6:
        return code
    if code.startswith(("60", "68", "9")):
        return code + ".SH"
    return code + ".SZ"


def _candidates_from_mysql_latest_day(
    min_change: float, max_change: float, max_price: float,
    min_vol_ratio: float, mcap_range: Tuple[float, float],
    min_listed_days: int, require_sector_resonance: bool, top_k: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """从 MySQL wucai_trade.* 拉「最近一个真实交易日」的全市场涨跌 + 板块共振.

    使用本 CASE 内 dragon_strategy/dragon_backtest 中的 SQL 拼装；任一步失败则抛错，由上层回退 xtdata / mock。

    返回: (候选列表, 真实交易日 yyyy-MM-dd)
    """
    import pandas as pd
    from datetime import date, timedelta

    # 1) 元信息
    meta = load_status_meta()
    if meta.empty:
        raise RuntimeError("trade_stock_status 为空, 请先同步 MySQL 元数据表")

    # 2) 拉最近 12 个自然日的日 K (足够覆盖一个交易日 + 5 日均量窗口, 节假日富余)
    end_d = date.today()
    start_d = end_d - timedelta(days=12)
    panel = load_daily_panel(start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))
    if panel.empty:
        raise RuntimeError(f"trade_stock_daily 在 {start_d}~{end_d} 区间为空, 请先跑日更入库")

    # 3) 拿最近一个真实交易日 (panel 里出现的最大日期)
    latest_t = panel["trade_date"].max()
    latest_str = pd.Timestamp(latest_t).strftime("%Y-%m-%d")

    # 4) 板块面板 (同区间, 用 sector_2 = 申万二级)
    sector_panel = load_sector_panel(start_d.strftime("%Y-%m-%d"),
                                      end_d.strftime("%Y-%m-%d"), sector_level=2)
    if sector_panel.empty and require_sector_resonance:
        raise RuntimeError("trade_sector_daily 为空, 无法做板块共振过滤; 请先准备板块日表")

    # 5) 复用 backtest 的拼装函数 (含 picker 完整 v1+v2 过滤 + dragon_score)
    cands = build_today_candidates(
        panel, meta, sector_panel, latest_t,
        min_change=min_change,
        max_change=max_change,
        max_price=max_price,
        min_vol_ratio=min_vol_ratio,
        mcap_range=mcap_range,
        min_listed_days=min_listed_days,
        require_sector_resonance=require_sector_resonance,
        top_k=top_k,
    )
    return cands, latest_str


def _candidates_from_xtdata(min_change: float, max_price: float,
                             min_vol_ratio: float) -> List[Dict[str, Any]]:
    """开盘时段从 xtdata 抓全市场涨跌, 拼出 dragon_picker 需要的字段。
    任意环节失败即抛出, 调用方回退到 mock。"""
    from xtquant import xtdata
    xtdata.connect()

    sectors = xtdata.get_stock_list_in_sector("沪深A股") or []
    if not sectors:
        raise RuntimeError("xtdata.get_stock_list_in_sector 返回空")

    ticks = xtdata.get_full_tick(sectors) or {}
    out: List[Dict[str, Any]] = []
    for code, t in ticks.items():
        try:
            last = float(t.get("lastPrice", 0))
            pre  = float(t.get("lastClose", 0))
            if last <= 0 or pre <= 0:
                continue
            chg = last / pre - 1.0
            vol = float(t.get("volume", 0))
            amt = float(t.get("amount", 0))
            # 用 amount/last 做近似流通市值兜底, 避免拉成分细节; 留给筛选区间过滤
            mcap = float(t.get("totalAmount", amt * 100))
            out.append({
                "code":            code,
                "name":            t.get("instrumentName", "") or "",
                "day_change_pct":  chg,
                "price":           last,
                "volume_ratio":    vol / max(amt / max(last, 1e-6), 1.0),
                "float_market_cap": mcap,
            })
        except Exception:
            continue
    if not out:
        raise RuntimeError("xtdata 候选构造为空")
    return out


@router.get("/candidates")
def candidates(
    source: str = Query("auto", description="auto / mysql / xtdata / mock"),
    use_xtdata: int = Query(0, description="(旧) 1=等价 source=xtdata"),
    min_change: float = Query(0.05),
    max_price: float = Query(30.0),
    min_vol_ratio: float = Query(2.0),
    require_resonance: int = Query(-1,
        description="v2 板块共振: 1=开 0=关 -1=按 source 自动 (mysql/mock 开, xtdata 关)"),
):
    """返回最近一个真实交易日的龙头候选名单 (按 dragon_score 降序, v2 含板块共振字段).

    source=auto (默认): 优先 mysql -> 失败回退 xtdata -> 再失败回退 mock.
    """
    # 兼容旧参数 use_xtdata=1
    if use_xtdata == 1 and source == "auto":
        source = "xtdata"
    source = (source or "auto").lower()
    if source not in ("auto", "mysql", "xtdata", "mock"):
        source = "auto"

    err: Optional[str] = None
    used_source = source
    trade_date: Optional[str] = None
    cands: List[Dict[str, Any]] = []

    # 共振决策表 (默认): mysql/mock 开, xtdata 关
    def _resolve_resonance(src: str) -> bool:
        if require_resonance == -1:
            return src in ("mysql", "mock")
        return bool(require_resonance)

    # ---------- 1) mysql 路径 ----------
    if source in ("auto", "mysql"):
        try:
            res_on = _resolve_resonance("mysql")
            cands, trade_date = _candidates_from_mysql_latest_day(
                min_change=min_change, max_change=0.095,
                max_price=max_price, min_vol_ratio=min_vol_ratio,
                mcap_range=(30e8, 500e8), min_listed_days=60,
                require_sector_resonance=res_on, top_k=20,
            )
            used_source = "mysql"
        except Exception as e:
            err = f"mysql: {type(e).__name__}: {e}"
            if source == "mysql":
                # 显式指定 mysql, 失败就报错, 不悄悄降级
                return {
                    "ok": False, "source": "mysql", "warning": err,
                    "count": 0, "items": [],
                }
            cands = []   # auto 模式继续往下试

    # ---------- 2) xtdata 路径 ----------
    if not cands and source in ("auto", "xtdata"):
        try:
            raw = _candidates_from_xtdata(min_change, max_price, min_vol_ratio)
            res_on = _resolve_resonance("xtdata")
            cands = filter_dragon_candidates(
                raw,
                min_change=min_change, max_price=max_price,
                mcap_range=(30e8, 500e8), min_volume_ratio=min_vol_ratio,
                require_sector_resonance=res_on,
            )
            used_source = "xtdata"
            err = None if used_source != "auto" else err  # auto 下保留 mysql 的告警链路
        except Exception as e:
            xt_err = f"xtdata: {type(e).__name__}: {e}"
            err = (err + " | " + xt_err) if err else xt_err
            if source == "xtdata":
                return {
                    "ok": False, "source": "xtdata", "warning": err,
                    "count": 0, "items": [],
                }

    # ---------- 3) mock 兜底 ----------
    if not cands:
        raw = _build_mock_today_stocks()
        res_on = _resolve_resonance("mock")
        cands = filter_dragon_candidates(
            raw,
            min_change=min_change, max_price=max_price,
            mcap_range=(30e8, 500e8), min_volume_ratio=min_vol_ratio,
            require_sector_resonance=res_on,
        )
        used_source = "mock"

    return {
        "ok":         True,
        "source":     used_source,
        "trade_date": trade_date,            # mysql 模式才有真实交易日
        "warning":    err,
        "params":     {
            "min_change": min_change, "max_price": max_price,
            "min_vol_ratio": min_vol_ratio,
            "require_sector_resonance": _resolve_resonance(used_source),
        },
        "count":      len(cands),
        "items":      cands[:20],
    }


@router.post("/bind")
def bind(payload: Optional[Dict[str, Any]] = Body(None)):
    """一键把若干候选股加入 watch_pool, 并把策略绑定到 dragon_picker (热加载)"""
    payload = payload or {}
    raw_codes = payload.get("codes") or []
    codes = [_ensure_market_dot(str(c)) for c in raw_codes if str(c).strip()]
    if not codes:
        return {"ok": False, "message": "codes 不能为空"}

    wp = load_watch_pool()
    pool = list(wp.get("codes") or [])
    added = []
    for c in codes:
        if c not in pool:
            pool.append(c)
            added.append(c)
    save_watch_pool(pool)

    cfg = load_strategy_config()
    per = dict(cfg.get("per_stock") or {})
    for c in codes:
        per[c] = DRAGON_STRATEGY
    default = cfg.get("default", "macd_5min")
    msg = _SIM.apply_strategy_config(default=default, per_stock=per)

    return {
        "ok":         True,
        "message":    f"已加入 {len(added)} 只新代码, 共 {len(codes)} 只绑定 {DRAGON_STRATEGY}。{msg}",
        "added":      added,
        "all_codes":  pool,
        "strategy":   DRAGON_STRATEGY,
    }
