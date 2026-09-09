# -*- coding: utf-8 -*-
# 板块轮动运行器（晨会内嵌）
from __future__ import annotations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .db_config import execute_query


def list_sectors(level: int = 2) -> List[Dict]:
    """从 trade_stock_status 反查某级别全部板块"""
    field = "sector_1" if level == 1 else "sector_2"
    sql = f"""
        SELECT {field} AS sector_name, COUNT(*) AS member_count
        FROM trade_stock_status
        WHERE {field} IS NOT NULL
        GROUP BY {field}
        ORDER BY {field}
    """
    return execute_query(sql)


def get_sector_member_codes(sector_name: str, level: int = 2) -> List[str]:
    """取板块当前成分股代码列表"""
    field = "sector_1" if level == 1 else "sector_2"
    rows = execute_query(
        f"SELECT stock_code FROM trade_stock_status WHERE {field} = %s ORDER BY stock_code",
        (sector_name,))
    return [r["stock_code"] for r in rows]


def load_sector_index(sector_name: str, level: int = 2,
                       end_date: Optional[str] = None) -> pd.DataFrame:
    """从 trade_sector_daily 加载单板块的合成指数 K 线"""
    conditions = ["sector_name = %s", "sector_level = %s"]
    params: list = [sector_name, level]
    if end_date:
        conditions.append("trade_date <= %s")
        params.append(end_date)

    sql = f"""
        SELECT trade_date, open_idx, high_idx, low_idx, close_idx,
               total_volume, total_amount, change_pct, stock_count
        FROM trade_sector_daily
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date ASC
    """
    rows = execute_query(sql, params)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.rename(columns={
        "open_idx": "open", "high_idx": "high",
        "low_idx": "low",   "close_idx": "close",
        "total_volume": "volume", "total_amount": "amount",
    }, inplace=True)
    for col in ["open", "high", "low", "close", "amount", "change_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_all_sectors(level: int = 2, end_date: Optional[str] = None,
                      min_days: int = 70) -> Dict[str, pd.DataFrame]:
    result = {}
    for s in list_sectors(level=level):
        df = load_sector_index(s["sector_name"], level=level, end_date=end_date)
        if not df.empty and len(df) >= min_days:
            result[s["sector_name"]] = df
    return result


def build_market_benchmark(sector_panel: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """全部板块等权 -> 市场基准"""
    if not sector_panel:
        return pd.DataFrame()
    closes  = pd.DataFrame()
    amounts = pd.DataFrame()
    for name, df in sector_panel.items():
        if df["close"].iloc[0] > 0:
            closes[name] = df["close"] / df["close"].iloc[0]
        amounts[name] = df["amount"]
    return pd.DataFrame({
        "close":  closes.mean(axis=1) * 1000,
        "amount": amounts.sum(axis=1),
    })


def calc_strength_indicators(sector_kline: pd.DataFrame,
                              market_kline: pd.DataFrame) -> Dict[str, float]:
    if len(sector_kline) < 65 or len(market_kline) < 65:
        return {}
    close = sector_kline["close"]
    amount = sector_kline["amount"]

    mom_21 = float(close.iloc[-1] / close.iloc[-22] - 1) if len(close) >= 22 else np.nan

    s_ret = close.pct_change(60).iloc[-1]
    m_ret = market_kline["close"].pct_change(60).iloc[-1]
    rs_60 = float(s_ret - m_ret) if pd.notna(s_ret) and pd.notna(m_ret) else np.nan

    avg_amt_5  = amount.tail(5).mean()
    avg_amt_60 = amount.tail(60).mean()
    vol_ratio = float(avg_amt_5 / avg_amt_60) if avg_amt_60 > 0 else np.nan

    return {"MOM_21": mom_21, "RS_60": rs_60, "VOL_RATIO": vol_ratio}


def _zscore(s: pd.Series) -> pd.Series:
    mu = s.mean()
    sd = s.std()
    if sd == 0 or pd.isna(sd):
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def calc_all_derivatives(close: pd.Series) -> Dict[str, float]:
    if len(close) < 60:
        return {}

    roc_20 = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else np.nan
    ma20 = close.rolling(20).mean()
    if len(ma20.dropna()) < 6:
        return {}
    ma20_slope = float((ma20.iloc[-1] - ma20.iloc[-6]) / ma20.iloc[-6]) * 100

    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    macd_hist = float((dif.iloc[-1] - dea.iloc[-1]) * 2)
    hist_prev = float((dif.iloc[-2] - dea.iloc[-2]) * 2) if len(dif) >= 2 else macd_hist
    hist_delta = macd_hist - hist_prev

    if len(ma20.dropna()) >= 11:
        slope_now  = (ma20.iloc[-1]  - ma20.iloc[-6])  / ma20.iloc[-6]  * 100
        slope_prev = (ma20.iloc[-6]  - ma20.iloc[-11]) / ma20.iloc[-11] * 100
        ma20_accel = float(slope_now - slope_prev)
    else:
        ma20_accel = np.nan

    return {
        "ROC_20":     roc_20,
        "MA20_SLOPE": ma20_slope,
        "MACD_HIST":  macd_hist,
        "HIST_DELTA": hist_delta,
        "MA20_ACCEL": ma20_accel,
    }


PHASE_DESC = {
    "accel_up":   "主升加速",
    "decel_up":   "高位钝化",
    "accel_down": "主跌",
    "decel_down": "左侧抄底",
    "neutral":    "中性",
}


def detect_phase(d: Dict[str, float]) -> Dict[str, str]:
    if not d:
        return {"phase": "neutral", "phase_desc": PHASE_DESC["neutral"],
                "vote_velocity": "", "vote_accel": ""}

    velocity_up = (d.get("ROC_20", 0) > 0.5 and d.get("MA20_SLOPE", 0) > 0.1)
    velocity_dn = (d.get("ROC_20", 0) < -0.5 and d.get("MA20_SLOPE", 0) < -0.1)
    accel_up    = (d.get("MACD_HIST", 0) > 0 and d.get("HIST_DELTA", 0) > 0
                   and d.get("MA20_ACCEL", 0) > 0)
    accel_dn    = (d.get("MACD_HIST", 0) < 0 and d.get("HIST_DELTA", 0) < 0
                   and d.get("MA20_ACCEL", 0) < 0)

    if velocity_up and accel_up:
        phase = "accel_up"
    elif velocity_up and accel_dn:
        phase = "decel_up"
    elif velocity_dn and accel_dn:
        phase = "accel_down"
    elif velocity_dn and accel_up:
        phase = "decel_down"
    else:
        phase = "neutral"

    return {
        "phase":         phase,
        "phase_desc":    PHASE_DESC[phase],
        "vote_velocity": "up" if velocity_up else ("down" if velocity_dn else "flat"),
        "vote_accel":    "up" if accel_up else ("down" if accel_dn else "flat"),
    }


PHASE_BONUS = {
    "accel_up":   3.0,
    "decel_down": 2.0,
    "decel_up":   0.5,
    "accel_down": -2.0,
    "neutral":    0.0,
}


def rank_industries_with_phase(level: int = 2,
                                end_date: Optional[str] = None,
                                lookback_days: int = 90,
                                top_n: int = 10) -> pd.DataFrame:
    panel = load_all_sectors(level=level, end_date=end_date)
    if not panel:
        return pd.DataFrame()

    bench = build_market_benchmark(panel)
    if bench.empty:
        return pd.DataFrame()

    sectors_meta = {s["sector_name"]: s for s in list_sectors(level=level)}

    rows = {}
    for name, df in panel.items():
        df_window = df.tail(max(lookback_days, 70))
        if len(df_window) < 70:
            continue
        bench_window = bench.loc[bench.index.intersection(df_window.index)]

        ind = calc_strength_indicators(df_window, bench_window)
        if not ind:
            continue
        derivs = calc_all_derivatives(df_window["close"])
        phase_info = detect_phase(derivs)

        rows[name] = {
            **ind,
            **derivs,
            **phase_info,
            "member_count": sectors_meta.get(name, {}).get("member_count", 0),
        }

    df_all = pd.DataFrame.from_dict(rows, orient="index").dropna(
        subset=["MOM_21", "RS_60", "VOL_RATIO"])
    if df_all.empty:
        return df_all

    df_all["MOM_21_z"]    = _zscore(df_all["MOM_21"])
    df_all["RS_60_z"]     = _zscore(df_all["RS_60"])
    df_all["VOL_RATIO_z"] = _zscore(df_all["VOL_RATIO"])
    df_all["score"]       = df_all["MOM_21_z"] + df_all["RS_60_z"] + 0.5 * df_all["VOL_RATIO_z"]
    df_all = df_all.sort_values("score", ascending=False)
    df_all["rank"] = range(1, len(df_all) + 1)

    df_all["phase_bonus"]     = df_all["phase"].map(PHASE_BONUS).fillna(0.0)
    df_all["composite_score"] = df_all["score"] + df_all["phase_bonus"]
    df_all = df_all.sort_values("composite_score", ascending=False)
    df_all["composite_rank"]  = range(1, len(df_all) + 1)

    return df_all.head(top_n) if top_n else df_all


def rank_industries(level: int = 2, lookback_days: int = 90, top_n: int = 10):
    return rank_industries_with_phase(level=level,
                                        lookback_days=lookback_days,
                                        top_n=top_n)
