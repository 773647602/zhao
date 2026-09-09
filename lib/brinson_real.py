# -*- coding: utf-8 -*-
# 25-AI量化系统 真实数据 Brinson 归因
"""
brinson_real -- 用模拟盘当前持仓 + 沪深300 基准 跑真实数据 Brinson 归因

工作流程:
    1. build_industry_map()  - 从 miniQMT 拉申万一级行业列表 + 反向映射 (cache 7 天)
    2. _portfolio_side()     - 读 sim 持仓 (live_state.json), 按行业聚合权重 + 区间收益
    3. _benchmark_side()     - 拉沪深300 当期成分股, 等权基准, 算各行业区间收益
    4. compute_real_brinson()- 调 24 章 brinson_attribution() 出三因子拆解

V1 简化策略 (设计文档同步):
    - 基准: 沪深300, 行业内等权 (不用市值加权, 简化数据接入)
    - 持仓快照: 用调用时刻的当前持仓 (不做时段加权 -- sim 区间一般不长)
    - 境外标的 (eg. 513100.SH 纳指 ETF / 港股 / QDII): 归到 "境外/其他" 分组,
      参与组合权重但 benchmark 那边对应权重 0 (会进入"配置效应")
    - 行业字典 cache: outputs/sw1_industry_map.json, TTL=7 天 (申万归属基本不动)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from lib.paths import setup_sys_path, OUTPUTS_DIR, OUTPUTS_LIVE_STATE
setup_sys_path()


# ============================================================
# 常量
# ============================================================

# 申万一级行业 cache 文件 + TTL
_INDUSTRY_CACHE_PATH = OUTPUTS_DIR / "sw1_industry_map.json"
_INDUSTRY_CACHE_TTL_DAYS = 7

# 默认基准
DEFAULT_BENCHMARK = "沪深300"

# 不属于申万一级体系的标的, 归到此 bucket (eg. ETF / QDII / 港股)
OTHER_BUCKET = "境外/其他"


# ============================================================
# 行业字典: 申万一级 (xtdata) + cache
# ============================================================

def _load_industry_cache() -> Optional[Dict[str, Any]]:
    """读取 cache; 过期返回 None"""
    if not _INDUSTRY_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_INDUSTRY_CACHE_PATH.read_text(encoding="utf-8"))
        built_at = data.get("built_at")
        if not built_at:
            return None
        built_dt = datetime.fromisoformat(built_at)
        if datetime.now() - built_dt > timedelta(days=_INDUSTRY_CACHE_TTL_DAYS):
            return None  # 过期
        return data
    except Exception:
        return None


def _save_industry_cache(data: Dict[str, Any]) -> None:
    _INDUSTRY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _INDUSTRY_CACHE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_industry_map(force_refresh: bool = False) -> Dict[str, Any]:
    """构建并缓存申万一级行业字典

    Returns: {
        "built_at":      ISO timestamp,
        "industries":    [行业短名 list], eg. ["电子", "食品饮料", ...]
        "ind_to_codes":  {行业短名: [stock codes]},
        "code_to_ind":   {stock code: 行业短名},  # 反向映射, 主用
        "hs300":         [沪深300 当期 300 个 stock codes],
    }

    数据来源: xtquant.xtdata.get_sector_list() + get_stock_list_in_sector()
    cache 文件: outputs/sw1_industry_map.json (TTL 7 天)
    """
    if not force_refresh:
        cache = _load_industry_cache()
        if cache is not None:
            return cache

    # 实际拉数据 - 必须 connect xtdata
    from xtquant import xtdata
    xtdata.connect()

    all_sectors = xtdata.get_sector_list() or []

    # 申万一级: 'SW1xxx', 排除"加权"版
    sw1_sectors = [s for s in all_sectors
                   if s.startswith("SW1") and not s.endswith("加权")]

    ind_to_codes: Dict[str, List[str]] = {}
    code_to_ind: Dict[str, str] = {}
    for sector in sw1_sectors:
        members = xtdata.get_stock_list_in_sector(sector) or []
        if not members:
            continue
        # 短名: SW1电子 -> 电子
        ind_short = sector.replace("SW1", "", 1) or sector
        ind_to_codes[ind_short] = members
        for code in members:
            code_to_ind[code] = ind_short

    # 沪深300 当期成分股
    hs300 = xtdata.get_stock_list_in_sector("沪深300") or []

    data = {
        "built_at":     datetime.now().isoformat(timespec="seconds"),
        "industries":   sorted(ind_to_codes.keys()),
        "ind_to_codes": ind_to_codes,
        "code_to_ind":  code_to_ind,
        "hs300":        sorted(hs300),
        "sw1_count":    len(ind_to_codes),
    }
    _save_industry_cache(data)
    return data


# ============================================================
# 工具: 算单只股票区间收益 (用 MySQL 日 K, 复用 backtest_data)
# ============================================================

def _stock_return(code: str, start: str, end: str,
                  cache: Optional[Dict[str, float]] = None) -> Optional[float]:
    """算单只股票从 start 开盘到 end 收盘的区间收益率

    取数: backtest_data.load_daily_kline (MySQL 优先, 失败回退 xtdata)
    Returns: 区间收益率 (eg. 0.05 = +5%); 数据不足返回 None
    """
    if cache is not None and code in cache:
        return cache[code]
    try:
        from lib.backtest_data import load_daily_kline
        df = load_daily_kline(code, start_date=start, end_date=end)
    except Exception:
        if cache is not None:
            cache[code] = None  # 标记缺失避免重复尝试
        return None
    if df is None or df.empty or len(df) < 2:
        if cache is not None:
            cache[code] = None
        return None
    # 区间收益: end 收盘 / start 开盘 - 1
    open0 = float(df["open"].iloc[0])
    close1 = float(df["close"].iloc[-1])
    if open0 <= 0:
        return None
    ret = close1 / open0 - 1.0
    if cache is not None:
        cache[code] = ret
    return ret


# ============================================================
# 组合侧: 模拟盘当前持仓 -> 行业权重 + 行业收益
# ============================================================

def _load_sim_positions() -> List[Dict[str, Any]]:
    """读 sim 当前持仓 (live_state.json)
    Returns: [{code, name, volume, cost, cur_price, market_value, ...}, ...]
    """
    if not OUTPUTS_LIVE_STATE.exists():
        return []
    try:
        state = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
        return state.get("positions") or []
    except Exception:
        return []


def _load_real_positions() -> List[Dict[str, Any]]:
    """读 real 当前持仓 (miniQMT 实盘账户, 只读)

    miniQMT 字段: stock_code / volume / can_use_volume / open_price / market_value
    -> 适配为 sim 同款: code / cost / cur_price / volume / market_value / name

    cur_price = market_value / volume (反推, miniQMT 没直接给最新价)
    cost      = open_price (持仓平均成本)
    """
    import os as _os
    qmt_path = _os.getenv("QMT_PATH")
    account_id = _os.getenv("ACCOUNT_ID")
    if not qmt_path or not account_id:
        raise RuntimeError("QMT_PATH / ACCOUNT_ID 未配置 (.env)")

    from miniqmt_trader_v2 import MiniQMTTraderV2  # type: ignore
    trader = MiniQMTTraderV2(
        qmt_path=qmt_path,
        account_id=account_id,
        enable_heartbeat=False,
        enable_reconnect=False,
    )
    trader.connect()
    raw = trader.query_positions() or []

    # 字段适配 + 拉中文名
    try:
        from lib.backtest_data import get_stock_name
    except Exception:
        get_stock_name = lambda c: c   # noqa

    out = []
    for p in raw:
        vol = float(p.get("volume") or 0)
        if vol <= 0:
            continue
        mv = float(p.get("market_value") or 0)
        cost = float(p.get("open_price") or 0)
        cur_price = (mv / vol) if vol > 0 else cost
        code = p.get("stock_code") or ""
        out.append({
            "code":         code,
            "name":         get_stock_name(code) if code else "",
            "volume":       vol,
            "cost":         cost,
            "cur_price":    cur_price,
            "market_value": mv,
        })
    return out


def _portfolio_side(positions: List[Dict[str, Any]],
                    code_to_ind: Dict[str, str],
                    start: str, end: str,
                    return_cache: Dict[str, float]
                    ) -> Tuple[Dict[str, float], Dict[str, float], List[Dict[str, Any]]]:
    """组合侧: 行业权重 Wp + 行业收益 Rp + 明细

    Wp_i = 行业 i 持仓市值 / 总持仓市值
    Rp_i = 行业 i 内部各股票按市值权重加权的区间收益

    Returns:
        Wp:        {行业: 权重}
        Rp:        {行业: 加权收益}
        positions_detail: 持仓明细 [{code, industry, market_value, weight, ret}]
    """
    if not positions:
        return {}, {}, []

    # 1) 算每只股票市值 (用 cur_price 优先, 否则 cost)
    rows: List[Dict[str, Any]] = []
    total_mv = 0.0
    for p in positions:
        code = (p.get("code") or "").strip()
        if not code:
            continue
        volume = float(p.get("volume") or 0)
        if volume <= 0:
            continue
        cur_price = p.get("cur_price")
        cost = p.get("cost")
        # 优先 cur_price (反映最新行情), fallback 用 cost (零持仓时算 cost)
        try:
            price = float(cur_price) if cur_price else float(cost or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        mv = volume * price
        ind = code_to_ind.get(code, OTHER_BUCKET)
        ret = _stock_return(code, start, end, cache=return_cache)
        rows.append({
            "code":         code,
            "name":         p.get("name") or "",
            "industry":     ind,
            "volume":       volume,
            "price":        price,
            "market_value": mv,
            "ret":          ret if ret is not None else 0.0,
            "ret_missing":  ret is None,
        })
        total_mv += mv

    if total_mv <= 0:
        return {}, {}, rows

    # 2) 行业聚合: Wp = sum(mv) / total; Rp = sum(mv * ret) / sum(mv)
    ind_mv: Dict[str, float] = {}
    ind_mv_ret: Dict[str, float] = {}
    for r in rows:
        ind = r["industry"]
        ind_mv[ind] = ind_mv.get(ind, 0.0) + r["market_value"]
        ind_mv_ret[ind] = ind_mv_ret.get(ind, 0.0) + r["market_value"] * r["ret"]
        r["weight"] = r["market_value"] / total_mv  # 加进明细方便前端

    Wp = {ind: mv / total_mv for ind, mv in ind_mv.items()}
    Rp = {ind: (ind_mv_ret[ind] / ind_mv[ind]) for ind in ind_mv}
    return Wp, Rp, rows


# ============================================================
# 基准侧: 沪深300 当期成分股 -> 行业权重 + 行业等权收益
# ============================================================

def _benchmark_side(hs300_codes: List[str],
                    code_to_ind: Dict[str, str],
                    start: str, end: str,
                    return_cache: Dict[str, float]
                    ) -> Tuple[Dict[str, float], Dict[str, float], int]:
    """基准侧: 沪深300 行业权重 Wb + 行业等权收益 Rb

    V1 简化:
        Wb_i = 沪深300 中行业 i 的成分股数 / 300 (等权, 不按市值)
        Rb_i = 沪深300 中行业 i 的成分股区间收益等权均值

    Returns:
        Wb, Rb, valid_count (有效成分股数, 缺数据的会被剔除)
    """
    # 收集每只股票的行业 + 收益
    by_ind: Dict[str, List[float]] = {}
    valid = 0
    for code in hs300_codes:
        ret = _stock_return(code, start, end, cache=return_cache)
        if ret is None:
            continue  # 数据缺失剔除 (退市 / 停牌 / 新股)
        ind = code_to_ind.get(code, OTHER_BUCKET)
        by_ind.setdefault(ind, []).append(ret)
        valid += 1

    if valid == 0:
        return {}, {}, 0

    Wb = {ind: len(rets) / valid for ind, rets in by_ind.items()}
    Rb = {ind: (sum(rets) / len(rets)) for ind, rets in by_ind.items()}
    return Wb, Rb, valid


# ============================================================
# 工具: 拉某只票在 end 日 (或之前最近交易日) 的收盘价
# ============================================================

def _close_at(code: str, date_str: str,
              cache: Optional[Dict[str, float]] = None) -> Optional[float]:
    """拉某只股票在 date_str (YYYY-MM-DD) 那天的收盘价

    若 date_str 是非交易日 (周末/节假日), 向前找最近交易日 (7 天窗口足够覆盖长假).
    专门给 mark-to-market 期末持仓用 -- 跟 _stock_return (拉区间) 相对应.
    """
    if cache is not None and code in cache:
        return cache[code]
    try:
        from lib.backtest_data import load_daily_kline
        from datetime import datetime as _dt, timedelta
        end_dt = _dt.fromisoformat(date_str)
        start_dt = end_dt - timedelta(days=7)
        df = load_daily_kline(
            code,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=date_str,
        )
    except Exception:
        if cache is not None:
            cache[code] = None
        return None
    if df is None or df.empty:
        if cache is not None:
            cache[code] = None
        return None
    close = float(df["close"].iloc[-1])
    if cache is not None:
        cache[code] = close
    return close


# ============================================================
# 组合侧 (交割单流口径): 从买入/卖出流水算"区间内做这些交易净赚多少"
# ============================================================

def _portfolio_side_from_trades(trades_df: pd.DataFrame,
                                code_to_ind: Dict[str, str],
                                start: str, end: str,
                                close_cache: Dict[str, float]
                                ) -> Tuple[Dict[str, float], Dict[str, float], List[Dict[str, Any]]]:
    """从交割单流水算组合侧 (Wp, Rp, 明细)

    口径 (跟 _portfolio_side 快照法不同):
        - 区间内已平仓的票: 卖出金额 - 买入金额 = 已实现 PnL
        - 区间末仍持仓的票: 净仓位 * end 收盘价 = 期末市值, mark-to-market
        - 单只总损益 = 卖出 + 期末市值 - 买入
        - 单只收益率 = 单只总损益 / 累计买入金额
        - Wp[ind] = 行业累计买入 / 总累计买入   (按"投入占比"权重)
        - Rp[ind] = 行业总损益 / 行业累计买入   (买入金额加权收益率)

    为什么用"买入金额"做权重 (而不是市值快照):
        - 交割单里没有持仓快照, 只有现金流
        - "投了多少钱在这个行业" 是更直观的"想法权重"
        - 跟 Wb (沪深300 行业内成分股数量占比) 同样是"占比"概念, 维度一致
    """
    if trades_df is None or trades_df.empty:
        return {}, {}, []

    # 1) 区间过滤 (字符串日期 YYYY-MM-DD 字典序就是日期序)
    df = trades_df[
        (trades_df["trade_date"].astype(str) >= start)
        & (trades_df["trade_date"].astype(str) <= end)
    ].copy()
    if df.empty:
        return {}, {}, []

    rows_detail: List[Dict[str, Any]] = []
    for code, sub in df.groupby("code"):
        buy_mask = sub["side"].astype(str).str.lower() == "buy"
        sell_mask = sub["side"].astype(str).str.lower() == "sell"
        buy_qty  = float(sub.loc[buy_mask,  "quantity"].sum())
        sell_qty = float(sub.loc[sell_mask, "quantity"].sum())
        buy_amt  = float(sub.loc[buy_mask,  "amount"].sum())
        sell_amt = float(sub.loc[sell_mask, "amount"].sum())
        net_qty  = buy_qty - sell_qty

        # 期末市值: 净仓位 * end 收盘价 (mark-to-market). 净仓 0 / 负 直接置 0
        end_close: Optional[float] = None
        end_value = 0.0
        if net_qty > 0:
            end_close = _close_at(str(code), end, cache=close_cache)
            end_value = net_qty * end_close if end_close else 0.0

        total_pnl = sell_amt + end_value - buy_amt
        ret = (total_pnl / buy_amt) if buy_amt > 0 else 0.0
        ind = code_to_ind.get(str(code), OTHER_BUCKET)

        # 名字 (CSV 里 name 列)
        name_col = "name" if "name" in sub.columns else None
        name = str(sub[name_col].iloc[0]) if name_col else ""

        rows_detail.append({
            "code":      str(code),
            "name":      name,
            "industry":  ind,
            "buy_qty":   buy_qty,
            "sell_qty":  sell_qty,
            "net_qty":   net_qty,
            "buy_amt":   buy_amt,
            "sell_amt":  sell_amt,
            "end_close": end_close,
            "end_value": end_value,
            "total_pnl": total_pnl,
            "ret":       ret,
            "closed":    net_qty <= 0,        # 期末是否已平仓
        })

    # 2) 行业聚合
    total_buy = sum(r["buy_amt"] for r in rows_detail)
    if total_buy <= 0:
        return {}, {}, rows_detail

    ind_buy: Dict[str, float] = {}
    ind_pnl: Dict[str, float] = {}
    for r in rows_detail:
        ind = r["industry"]
        ind_buy[ind] = ind_buy.get(ind, 0.0) + r["buy_amt"]
        ind_pnl[ind] = ind_pnl.get(ind, 0.0) + r["total_pnl"]

    Wp = {ind: amt / total_buy for ind, amt in ind_buy.items()}
    Rp = {ind: (ind_pnl[ind] / ind_buy[ind]) for ind in ind_buy}
    return Wp, Rp, rows_detail


# ============================================================
# 主入口: 真实数据 Brinson
# ============================================================

def compute_real_brinson(start: str, end: str,
                         benchmark: str = DEFAULT_BENCHMARK,
                         source: str = "sim") -> Dict[str, Any]:
    """对模拟盘 (或后续实盘) 当前持仓跑真实数据 Brinson 归因

    Args:
        start:     'YYYY-MM-DD' 区间起始 (含)
        end:       'YYYY-MM-DD' 区间结束 (含)
        benchmark: 目前只支持 '沪深300' (V1)
        source:    'sim' (模拟盘 live_state.json) | 'real' (miniQMT 实盘只读)

    Returns:
        {
            "ok":               bool,
            "message":          str,
            "source":           "sim",
            "benchmark":        "沪深300",
            "params":           {start, end, asof},
            "industry_map":     {built_at, sw1_count, hs300_count},
            "positions_detail": [{code, name, industry, weight, ret, ret_missing}],
            "portfolio": {       # Brinson 标准输入 (组合侧)
                "weights":  {行业: Wp},
                "returns":  {行业: Rp},
            },
            "benchmark": {
                "weights":  {行业: Wb},
                "returns":  {行业: Rb},
                "valid_count": int,    # 实际算入的沪深300 成分股数
            },
            "result":  {            # 跟 demo 接口同结构, 前端可直接复用
                "portfolio_return":   float,
                "benchmark_return":   float,
                "excess_return":      float,
                "allocation_effect":  float,
                "selection_effect":   float,
                "interaction_effect": float,
                "by_industry":        [{industry, Wp, Wb, Rp, Rb,
                                        allocation, selection, interaction, total}],
            },
        }
    """
    t0 = time.time()
    if benchmark != DEFAULT_BENCHMARK:
        return {"ok": False, "message": f"V1 只支持基准 {DEFAULT_BENCHMARK}, 收到 {benchmark}"}
    if source not in ("sim", "real"):
        return {"ok": False, "message": f"source 必须是 sim 或 real, 收到 {source}"}

    # 1) 行业字典
    try:
        ind_map = build_industry_map()
    except Exception as e:
        return {"ok": False, "message": f"拉申万一级行业失败: {type(e).__name__}: {e}"}
    code_to_ind = ind_map.get("code_to_ind") or {}
    hs300 = ind_map.get("hs300") or []
    if not code_to_ind or not hs300:
        return {"ok": False, "message": "申万一级 / 沪深300 数据为空, 检查 miniQMT 是否启动"}

    # 2) 持仓 (sim 或 real)
    if source == "sim":
        positions = _load_sim_positions()
        if not positions:
            return {"ok": False, "message": "live_state.json 没有 positions, 模拟盘可能未启动"}
    else:
        try:
            positions = _load_real_positions()
        except Exception as e:
            return {"ok": False, "message": f"读 real 账户持仓失败 (miniQMT): {type(e).__name__}: {e}"}
        if not positions:
            return {"ok": False, "message": "实盘账户当前无持仓 (miniQMT 返回空)"}

    # 3) 区间收益 cache (一只票只拉一次)
    return_cache: Dict[str, float] = {}

    # 4) 组合侧
    Wp, Rp, pos_detail = _portfolio_side(positions, code_to_ind, start, end, return_cache)
    if not Wp:
        return {"ok": False, "message": "组合侧权重为空 (持仓市值=0?)"}

    # 5) 基准侧
    Wb, Rb, valid_bench = _benchmark_side(hs300, code_to_ind, start, end, return_cache)
    if not Wb:
        return {"ok": False, "message": "基准侧权重为空 (沪深300 数据缺失?)"}

    # 6) 调 24 章 brinson_attribution
    from attribution.brinson import brinson_attribution
    res = brinson_attribution(
        portfolio_weights=Wp,
        benchmark_weights=Wb,
        portfolio_returns=Rp,
        benchmark_returns=Rb,
    )

    # 7) 拼返回
    by_ind = []
    if hasattr(res, "by_industry"):
        df = res.by_industry
        for ind, row in df.iterrows():
            by_ind.append({
                "industry":    row.get("industry", ind),
                "Wp":          float(row.get("Wp", 0)),
                "Wb":          float(row.get("Wb", 0)),
                "Rp":          float(row.get("Rp", 0)),
                "Rb":          float(row.get("Rb", 0)),
                "allocation":  float(row.get("allocation", 0)),
                "selection":   float(row.get("selection", 0)),
                "interaction": float(row.get("interaction", 0)),
                "total":       float(row.get("total", 0)),
            })

    return {
        "ok":      True,
        "message": "OK",
        "source":  source,
        "benchmark": benchmark,
        "params": {
            "start": start,
            "end":   end,
            "asof":  datetime.now().isoformat(timespec="seconds"),
            "elapsed_sec": round(time.time() - t0, 2),
        },
        "industry_map": {
            "built_at":   ind_map.get("built_at"),
            "sw1_count":  ind_map.get("sw1_count"),
            "hs300_count": len(hs300),
        },
        "positions_detail": pos_detail,
        "portfolio": {"weights": Wp, "returns": Rp},
        "benchmark": {
            "weights":     Wb,
            "returns":     Rb,
            "valid_count": valid_bench,
        },
        "result": {
            "portfolio_return":   res.portfolio_return,
            "benchmark_return":   res.benchmark_return,
            "excess_return":      res.excess_return,
            "allocation_effect":  res.allocation_effect,
            "selection_effect":   res.selection_effect,
            "interaction_effect": res.interaction_effect,
            "by_industry":        by_ind,
        },
    }


# ============================================================
# 主入口 (交割单流口径): CSV 交割单 -> Brinson
# ============================================================

def compute_brinson_from_trades(csv_path: str,
                                start: str, end: str,
                                benchmark: str = DEFAULT_BENCHMARK
                                ) -> Dict[str, Any]:
    """从 CSV 交割单跑 Brinson 归因 (跟 compute_real_brinson 平级, 数据源不同)

    跟 compute_real_brinson 的区别:
        - 这里组合侧来自交割单流水 (买入/卖出现金流 + 期末市值 mark-to-market)
        - 那里组合侧来自当前持仓快照 (单点 snapshot)
        - 基准侧 (沪深300 等权 + 申万一级) 完全复用, 保证可比

    Args:
        csv_path: 券商导出的"历史成交"CSV (中文表头, attribution.trade_record.load_from_csv 兼容)
        start:    'YYYY-MM-DD' 区间起始 (含)
        end:      'YYYY-MM-DD' 区间结束 (含)
        benchmark: 目前只支持 '沪深300'

    Returns: 跟 compute_real_brinson 同结构, 多一个 trades_detail 字段
    """
    t0 = time.time()
    if benchmark != DEFAULT_BENCHMARK:
        return {"ok": False, "message": f"V1 只支持基准 {DEFAULT_BENCHMARK}, 收到 {benchmark}"}

    # 1) 加载交割单
    try:
        from attribution.trade_record import load_from_csv
        trades_df = load_from_csv(str(csv_path))
    except Exception as e:
        return {"ok": False, "message": f"加载 CSV 失败: {type(e).__name__}: {e}"}
    if trades_df is None or trades_df.empty:
        return {"ok": False, "message": f"CSV 无可用交易记录: {csv_path}"}

    # 2) 行业字典
    try:
        ind_map = build_industry_map()
    except Exception as e:
        return {"ok": False, "message": f"拉申万一级行业失败: {type(e).__name__}: {e}"}
    code_to_ind = ind_map.get("code_to_ind") or {}
    hs300 = ind_map.get("hs300") or []
    if not code_to_ind or not hs300:
        return {"ok": False, "message": "申万一级 / 沪深300 数据为空, 检查 miniQMT 是否启动"}

    # 3) 区间收益 cache (基准侧用) + 期末收盘价 cache (组合侧 mark-to-market 用)
    return_cache: Dict[str, float] = {}
    close_cache:  Dict[str, float] = {}

    # 4) 组合侧 (交割单流口径)
    Wp, Rp, trades_detail = _portfolio_side_from_trades(
        trades_df, code_to_ind, start, end, close_cache,
    )
    if not Wp:
        return {"ok": False,
                "message": f"区间 [{start}, {end}] 内 CSV 无交易记录, 调整起止日期再跑"}

    # 5) 基准侧 (复用 compute_real_brinson 的逻辑, 保证两个数据源可对比)
    Wb, Rb, valid_bench = _benchmark_side(hs300, code_to_ind, start, end, return_cache)
    if not Wb:
        return {"ok": False, "message": "基准侧权重为空 (沪深300 数据缺失?)"}

    # 6) 调核心 brinson_attribution
    from attribution.brinson import brinson_attribution
    res = brinson_attribution(
        portfolio_weights=Wp,
        benchmark_weights=Wb,
        portfolio_returns=Rp,
        benchmark_returns=Rb,
    )

    # 7) by_industry 拼接
    by_ind: List[Dict[str, Any]] = []
    if hasattr(res, "by_industry"):
        for ind, row in res.by_industry.iterrows():
            by_ind.append({
                "industry":    row.get("industry", ind),
                "Wp":          float(row.get("Wp", 0)),
                "Wb":          float(row.get("Wb", 0)),
                "Rp":          float(row.get("Rp", 0)),
                "Rb":          float(row.get("Rb", 0)),
                "allocation":  float(row.get("allocation", 0)),
                "selection":   float(row.get("selection", 0)),
                "interaction": float(row.get("interaction", 0)),
                "total":       float(row.get("total", 0)),
            })

    # 8) 交割单概要 (给前端展示用): 总投入 / 总平仓 / 期末市值 / 总损益
    total_buy   = sum(r["buy_amt"]   for r in trades_detail)
    total_sell  = sum(r["sell_amt"]  for r in trades_detail)
    total_endmv = sum(r["end_value"] for r in trades_detail)
    total_pnl   = sum(r["total_pnl"] for r in trades_detail)

    return {
        "ok":      True,
        "message": "OK",
        "source":  "csv",
        "benchmark": benchmark,
        "params": {
            "start": start,
            "end":   end,
            "asof":  datetime.now().isoformat(timespec="seconds"),
            "elapsed_sec": round(time.time() - t0, 2),
            "csv_path": str(csv_path),
        },
        "industry_map": {
            "built_at":    ind_map.get("built_at"),
            "sw1_count":   ind_map.get("sw1_count"),
            "hs300_count": len(hs300),
        },
        # 注意字段名跟 compute_real_brinson 区分: positions_detail vs trades_detail
        "trades_detail": trades_detail,
        "trades_summary": {
            "n_codes":       len(trades_detail),
            "total_buy":     total_buy,
            "total_sell":    total_sell,
            "total_end_mv":  total_endmv,
            "total_pnl":     total_pnl,
            "portfolio_ret": (total_pnl / total_buy) if total_buy > 0 else 0.0,
        },
        "portfolio": {"weights": Wp, "returns": Rp},
        "benchmark": {
            "weights":     Wb,
            "returns":     Rb,
            "valid_count": valid_bench,
        },
        "result": {
            "portfolio_return":   res.portfolio_return,
            "benchmark_return":   res.benchmark_return,
            "excess_return":      res.excess_return,
            "allocation_effect":  res.allocation_effect,
            "selection_effect":   res.selection_effect,
            "interaction_effect": res.interaction_effect,
            "by_industry":        by_ind,
        },
    }
