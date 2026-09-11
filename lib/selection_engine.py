# -*- coding: utf-8 -*-
# 多策略并行选股引擎
"""
selection_engine -- 从全市场(有日K的A股)按每策略各自的选股条件选出股票

- build_universe_snapshot(): 批量 SQL 一次性拉全市场日K面板, 派生出价/涨跌幅/量比/成交额等。
  非逐股 load_daily_kline (那样 5218 只太慢)。
- run_strategy_selection(inst, snapshot): 按策略 selection.mode 选股 + top_n。
    mode=registry_scan : 调策略自带 selector 打分降序 -> filters -> top_n
    mode=generic_filter : 声明式 filters 过滤 -> sort_by -> top_n (信号类策略无原生选股器)
    mode=code_list      : 直接取 code_list (可选附加 filters), 不跑全市场
- iter_strategy_signal_targets(): 供实盘循环遍历"每策略自己的候选+持仓" (买卖独立性)
"""
from __future__ import annotations

from functools import lru_cache
from datetime import datetime, date, timedelta
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from lib.paths import setup_sys_path

setup_sys_path()

from lib.backtest_data import _db_config, clear_kline_cache
from lib.strategy_registry import get_strategy


# 拉日K的最小起始日 (覆盖最长策略 lookback + buffer)
_LONGEST_LOOKBACK = 260


def _fetch_universe_daily(start_date: str) -> pd.DataFrame:
    """批量拉全市场 [start_date, 最新] 日K面板 (单条大 SQL, 非逐股)"""
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT stock_code, trade_date, open_price, high_price, low_price, "
            "close_price, volume, amount FROM trade_stock_daily WHERE trade_date >= %s "
            "ORDER BY stock_code, trade_date",
            (start_date,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame(columns=[
            "stock_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount",
        ])
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "open_price": "open", "high_price": "high",
        "low_price": "low", "close_price": "close",
    })
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["stock_code"] = df["stock_code"].astype(str)
    return df


def _bigqmt_full_tick() -> Dict[str, dict]:
    """一次批量拉全市场当日实时 tick (BigQMT 桥, 实测全市场 5218 只约 11s, 成功率100%).

    结果形式: {code: {open, lastPrice, lastClose, high, low, volume, amount, ...}}
    桥不可用时返回空 dict (不抛异常)。
    """
    try:
        import os
        import sys as _sys
        extra = Path(__file__).resolve().parent.parent / "third_party" / "xtquant_big_convert" / "src"
        cfg_dir = Path(__file__).resolve().parent.parent / "config"
        for p in (str(extra), str(cfg_dir)):
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient
        client = BigQmtRpcClient(
            account_id=os.environ.get("ACCOUNT_ID", ""),
            redis_config={"transport": "zmq",
                          "zmq": {"connect_address": "tcp://127.0.0.1:15615"}},
        )
        # codes 从 MySQL 全市场列表取
        import pymysql
        conn = pymysql.connect(**_db_config())
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily "
                        "WHERE trade_date=(SELECT MAX(trade_date) FROM trade_stock_daily)")
            codes = [r[0] for r in cur.fetchall()]
            cur.close()
        finally:
            conn.close()
        if not codes:
            return {}
        out = client.call("get_full_tick", {"codes": codes, "types": []})
        return out if isinstance(out, dict) else {}
    except Exception as e:
        print(f"[selection_engine] BigQMT 当日实时拉取失败, 回退日线: {type(e).__name__}: {e}",
              flush=True)
        return {}


def _inject_intraday_today(panel: pd.DataFrame) -> tuple:
    """把当日实时 tick 作为"今日"一行并入日K面板 (实时选股用).

    仅当: 未指定 asof_date(实时路径)、今日为工作日、且面板最新交易日在今日之前。
    返回 (新面板, 是否注入)。面板不可行时原样返回。
    """
    if panel is None or panel.empty:
        return panel, False
    today = datetime.now()
    if today.weekday() >= 5:            # 非交易日不注入
        return panel, False
    today_s = today.strftime("%Y-%m-%d")
    latest = panel["trade_date"].max()
    if str(latest.date()) == today_s:   # 当日日线已入库(收盘后), 无需注入
        return panel, False
    ticks = _bigqmt_full_tick()
    if not ticks:
        return panel, False
    today_str = today.strftime("%Y-%m-%d")
    rows = []
    for code, t in ticks.items():
        if not isinstance(t, dict):
            continue
        open_p = t.get("open")
        last = t.get("lastPrice")
        if open_p is None or last is None:
            continue
        rows.append({
            "stock_code": str(code),
            "trade_date": pd.Timestamp(today_str),
            "open": float(open_p),
            "high": float(t.get("high") or open_p),
            "low": float(t.get("low") or open_p),
            "close": float(last),
            "volume": float(t.get("volume") or 0),
            "amount": float(t.get("amount") or 0),
        })
    if not rows:
        return panel, False
    add = pd.DataFrame(rows)
    merged = pd.concat([panel, add], ignore_index=True)
    return merged, True


@lru_cache(maxsize=1)
def _panel(start_date: str) -> pd.DataFrame:
    return _fetch_universe_daily(start_date)


def _clear_panel() -> None:
    """清缓存, 让下一次获取强制拉最新"""
    _panel.cache_clear()
    try:
        clear_kline_cache()
    except Exception:
        pass


class UniverseSnapshot:
    """全市场日K快照: meta 为每股一行 (index=code, 含最新价/涨跌幅/量比/成交额等)"""

    def __init__(self, meta: pd.DataFrame, latest_date: str,
                 index_open_pct: Optional[float] = None,
                 index_pct: Optional[float] = None,
                 index_above_ma: Optional[Dict[int, bool]] = None,
                 index_prev_amount: Optional[float] = None):
        self.meta = meta
        self.latest_date = latest_date
        self.index_open_pct = index_open_pct  # 大盘(上证综指)当日开盘涨跌幅(%)
        self.index_pct = index_pct            # 大盘(上证综指)当日涨跌幅(%), 无数据为 None
        self.index_above_ma = index_above_ma or {}  # {period: 当日收盘是否站上N日线}
        self.index_prev_amount = index_prev_amount  # 昨日全市场(沪深两市)成交额(元)

    def codes(self) -> List[str]:
        return list(self.meta.index)

    def filtered_df(self, filters: Dict[str, Any]) -> pd.DataFrame:
        """应用声明式过滤, 返回过滤后的可按某列排序的 DataFrame"""
        df = self.meta.copy()
        if not df.empty:
            f = filters or {}
            # 大盘(上证指数当日"开盘"涨幅%)全局闸门: 开盘涨幅高于 阈值% 则不选票 (高于就停); null=禁用
            maxix = f.get("max_index_pct")
            if maxix is not None and (self.index_open_pct is None or self.index_open_pct > float(maxix)):
                return df.iloc[0:0]
            # 大盘(上证指数)昨日收盘站上 N 日线: 未站上则不选票; null=禁用
            imp = f.get("index_ma_period")
            if imp not in (None, "", 0):
                if not (self.index_above_ma or {}).get(int(imp)):
                    return df.iloc[0:0]
            # 情绪全局闸门: 昨日全市场"上涨家数"(昨收涨幅>0) > 阈值 则不选票(普涨过热); null=禁用
            mup = f.get("max_prev_up_count")
            if mup not in (None, "", 0) and "prev_pct" in df:
                _up = int((df["prev_pct"] > 0).sum())
                if _up > float(mup):
                    return df.iloc[0:0]
            # 量能全局闸门: 昨日全市场量能(沪深两市成交额) 低于 阈值万亿 则不选票(地量); null=禁用
            mm = f.get("min_prev_market_amount")
            if mm is not None and float(mm) > 0:
                _amt = self.index_prev_amount  # 昨日两市成交额(元)
                if _amt is None or _amt < float(mm) * 1e12:
                    return df.iloc[0:0]
            # 弱转强1: 昨日收盘涨幅 < 阈值% (昨日弱/收弱); null=禁用
            if f.get("max_prev_pct") is not None and "prev_pct" in df:
                df = df[df["prev_pct"] < float(f["max_prev_pct"])]
            # 弱转强2: 昨日开盘涨幅 < 阈值% (昨日开得不高/不虚高); null=禁用
            if f.get("max_prev_open_pct") is not None and "prev_open_pct" in df:
                df = df[df["prev_open_pct"] < float(f["max_prev_open_pct"])]
            # 弱转强2: 今日开盘涨幅 > 阈值% (今日转强/高开); null=禁用
            if f.get("min_open_pct") is not None and "open_pct" in df:
                df = df[df["open_pct"] > float(f["min_open_pct"])]
            # 弱转强3: 昨日量能较前日增幅 % (>=); null=禁用
            if f.get("min_vol_grow_pct") is not None and "vol_grow_pct" in df:
                df = df[df["vol_grow_pct"] >= float(f["min_vol_grow_pct"])]
            # 弱转强4: 昨日收盘站上 N 日线 (ma_period=5/10/20); null=禁用
            mper = f.get("ma_period")
            if mper is not None:
                col = f"above_ma{int(mper)}_prev"
                if col in df:
                    df = df[df[col] >= 1]

            if f.get("min_price") is not None and "close" in df:
                df = df[df["close"] >= float(f["min_price"])]
            if f.get("max_price") is not None and "close" in df:
                df = df[df["close"] <= float(f["max_price"])]
            if f.get("min_change_pct") is not None and "pct_change" in df:
                df = df[df["pct_change"] >= float(f["min_change_pct"])]
            if f.get("max_change_pct") is not None and "pct_change" in df:
                df = df[df["pct_change"] <= float(f["max_change_pct"])]
            if f.get("min_turnover") is not None and "turnover_pct" in df:
                df = df[df["turnover_pct"] >= float(f["min_turnover"])]
            if f.get("max_turnover") is not None and "turnover_pct" in df:
                df = df[df["turnover_pct"] <= float(f["max_turnover"])]
            if f.get("min_market_cap") is not None and "market_cap" in df:
                df = df[df["market_cap"] >= float(f["min_market_cap"])]
            if f.get("max_market_cap") is not None and "market_cap" in df:
                df = df[df["market_cap"] <= float(f["max_market_cap"])]
            if f.get("min_vol_ratio") is not None and "vol_ratio" in df:
                df = df[df["vol_ratio"] >= float(f["min_vol_ratio"])]
            # 板块排除: exclude_chinext=排除创业板(300/301开头 .SZ), exclude_star=排除科创板(688开头 .SH);
            # 空/False/null=不禁用 (保持可开关). index 为带后缀代码, 如 300750.SZ.
            if f.get("exclude_chinext") not in (None, "", False):
                df = df[~df.index.map(lambda c: str(c).split(".")[0]).str.startswith(("300", "301"))]
            if f.get("exclude_star") not in (None, "", False):
                df = df[~df.index.map(lambda c: str(c).split(".")[0]).str.startswith("688")]
            excl = f.get("excluded_codes") or []
            if excl:
                df = df[~df.index.isin([str(c).strip() for c in excl if str(c).strip()])]
        return df


_INDEX_LIVE_CACHE: dict = {"data": None, "ts": 0.0}
_INDEX_LIVE_TTL = 6.0  # 秒; 盘中指数快照缓存, 避免每次触发都打桥


def _index_live_tick() -> Optional[dict]:
    """上证指数(000001.SH)当日实时 tick (BigQMT 桥). 带 6s TTL 缓存, 桥不可用返回 None."""
    now = time.time()
    if _INDEX_LIVE_CACHE["data"] is not None and (now - _INDEX_LIVE_CACHE["ts"]) < _INDEX_LIVE_TTL:
        return _INDEX_LIVE_CACHE["data"]
    try:
        import os
        import sys as _sys
        extra = Path(__file__).resolve().parent.parent / "third_party" / "xtquant_big_convert" / "src"
        cfg_dir = Path(__file__).resolve().parent.parent / "config"
        for p in (str(extra), str(cfg_dir)):
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient
        client = BigQmtRpcClient(
            account_id=os.environ.get("ACCOUNT_ID", ""),
            redis_config={"transport": "zmq",
                          "zmq": {"connect_address": "tcp://127.0.0.1:15615"}},
        )
        out = client.call("get_full_tick", {"codes": ["000001.SH"], "types": []})
        t = (out or {}).get("000001.SH")
        if not isinstance(t, dict):
            raise ValueError("指数 tick 为空")
        _INDEX_LIVE_CACHE["data"] = t
        _INDEX_LIVE_CACHE["ts"] = now
        return t
    except Exception as e:
        print(f"[WARN] 上证指数实时 tick 获取失败: {e}", flush=True)
        return None


@lru_cache(maxsize=31)
def _cached_index_open_pct(day: Optional[str] = None) -> Optional[float]:
    """上证综指(000001.SH)"回放到 day 当日"开盘涨跌幅(%), 即 (day开盘÷前一日收盘-1)*100; day=None 取最新; 失败返回 None."""
    if day is None:
        t = _index_live_tick()
        if t and t.get("open") and t.get("lastClose"):
            lc = float(t["lastClose"])
            if lc > 0:
                op = float(t["open"])
                return (op / lc - 1.0) * 100.0
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or len(df) < 2:
            return None
        df = df.dropna(subset=["open", "close"]).sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        if day:
            sub = df[df["date"] <= pd.to_datetime(day)]
            if len(sub) >= 2:
                df = sub
        open_cur = float(df.iloc[-1]["open"])
        close_prev = float(df.iloc[-2]["close"])
        if close_prev <= 0:
            return None
        return (open_cur / close_prev - 1.0) * 100.0
    except Exception as e:
        print(f"[WARN] 上证指数开盘涨幅获取失败: {e}", flush=True)
        return None


@lru_cache(maxsize=31)
def _cached_index_pct(day: Optional[str] = None) -> Optional[float]:
    """上证综指"回放到 day 当日"涨跌幅(%), 即 (day收盘÷前一日收盘-1)*100; day=None 取最新; 失败返回 None."""
    if day is None:
        t = _index_live_tick()
        if t and t.get("lastPrice") and t.get("lastClose"):
            lc = float(t["lastClose"])
            if lc > 0:
                cur = float(t["lastPrice"])
                return (cur / lc - 1.0) * 100.0
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or len(df) < 2:
            return None
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        if day:
            sub = df[df["date"] <= pd.to_datetime(day)]
            if len(sub) >= 2:
                df = sub
        c_cur = float(df.iloc[-1]["close"])
        c_prev = float(df.iloc[-2]["close"])
        if c_prev <= 0:
            return None
        return (c_cur / c_prev - 1.0) * 100.0
    except Exception as e:
        print(f"[WARN] 上证指数涨幅获取失败: {e}", flush=True)
        return None


@lru_cache(maxsize=64)
def _cached_index_above_ma(day: Optional[str], period: int) -> Optional[bool]:
    """上证综指"回放到 day 当日"的昨日收盘是否站上 N 日线 (昨日收盘 > 前N日收盘均值, 不含昨日)。
    与个股 above_maN_prev 同口径: 用截止到"昨日"的前 N 根收盘算均线, 昨日收盘与其比较。
    失败返回 None (视为未站上)。"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or len(df) < period + 1:
            return None
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        if day:
            sub = df[df["date"] <= pd.to_datetime(day)]
            if len(sub) < period + 1:
                return None
            df = sub
        closes = df["close"].astype(float)
        prev_close = float(closes.iloc[-2])       # 昨日收盘
        ma = float(closes.iloc[-(period + 1):-1].mean())  # 昨日之前 N 日收盘均值
        return prev_close > ma
    except Exception as e:
        print(f"[WARN] 上证指数昨日站上{period}日线判断失败: {e}", flush=True)
        return None


@lru_cache(maxsize=64)
def _index_above_ma_map(day: Optional[str]) -> Dict[int, bool]:
    """回放到 day 当日, 上证综指收盘对 5/10/20 日线的站上情况 {period: bool}"""
    return {p: bool(_cached_index_above_ma(day, p)) for p in (5, 10, 20)}


@lru_cache(maxsize=64)
def _cached_index_prev_amount(date_str: Optional[str]) -> Optional[float]:
    """昨日全市场量能: trade_market_volume 中该交易日的沪深两市成交总额(元). 缺失/异常返回 None."""
    if not date_str:
        return None
    try:
        from lib.market_metrics import query_market_volume
        rows = query_market_volume(date_str)
        if rows:
            tot = rows[0].get("total_amount") or 0
            return float(tot) if tot > 0 else None
    except Exception as e:
        print(f"[WARN] 昨日全市场量能获取失败({date_str}): {e}", flush=True)
    return None


def build_universe_snapshot(lookback_days: int = _LONGEST_LOOKBACK,
                            asof_date: Optional[str] = None) -> UniverseSnapshot:
    """构建全市场快照: 每股一行, 以"asof_date 当日"为今日(>该日数据被截断)、前一根为昨日、再前为前日。
    asof_date=None 时逐个股用最新交易日 (历史回放/当日选股通用)。
    """
    lookback_days = max(int(lookback_days or 120), 30)
    start = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    panel = _panel(start)
    if not panel.empty and asof_date:
        ad = pd.Timestamp(asof_date)
        sub = panel[panel["trade_date"] <= ad]
        if not sub.empty:
            panel = sub
    # 实时选股 (asof_date=None): 若有当日盘中数据, 把"今日"一行并入面板 (仅本次局部)
    if asof_date is None:
        panel, _nothing = _inject_intraday_today(panel)
    if panel.empty:
        return UniverseSnapshot(pd.DataFrame(index=pd.Index([], name="code")),
                            date.today().strftime("%Y-%m-%d"),
                            index_open_pct=_cached_index_open_pct(asof_date),
                            index_pct=_cached_index_pct(asof_date),
                            index_above_ma=_index_above_ma_map(asof_date))

    latest_date = panel["trade_date"].max().strftime("%Y-%m-%d")
    # 昨日全市场量能: 用面板倒数第二个交易日作为"昨日"查询两市成交额
    _panel_dates = sorted(panel["trade_date"].unique())
    _prev_td = _panel_dates[-2].strftime("%Y-%m-%d") if len(_panel_dates) >= 2 else None
    _prev_amount = _cached_index_prev_amount(_prev_td) if _prev_td else None
    # 每股一个分组: 算最新价、当日涨跌幅、量比(最新量/20日均量)、成交额
    recs = []
    for code, g in panel.groupby("stock_code"):
        g = g.sort_values("trade_date").dropna(subset=["close"])
        if g.empty:
            continue
        last = g.iloc[-1]
        close = float(last["close"])
        if close <= 0:
            continue
        prev = g.iloc[-2]["close"] if len(g) >= 2 else None
        pct = (close / float(prev) - 1.0) * 100.0 if prev else None
        v20 = g["volume"].tail(20).mean() if len(g) else None
        vol_ratio = (float(last["volume"]) / float(v20)) if v20 else None
        # ---- 弱转强: 昨日/前日/多均线 指标 ----
        if len(g) >= 2:
            prev_open = float(g.iloc[-2]["open"])
            prev_close = float(g.iloc[-2]["close"])
            prev_vol = float(g.iloc[-2]["volume"])
            prev2_close = float(g.iloc[-3]["close"]) if len(g) >= 3 else None
        else:
            prev_open = prev_close = prev_vol = prev2_close = None
        prev2_vol = float(g.iloc[-3]["volume"]) if len(g) >= 3 else None
        vol_grow_pct = ((prev_vol / prev2_vol) - 1.0) * 100.0 \
            if (prev_vol is not None and prev2_vol) else None
        # 今日开盘涨幅% (今开/昨收-1)
        open_pct = (float(last["open"]) / prev_close - 1.0) * 100.0 \
            if (prev_close and prev_close > 0 and last["open"] is not None) else None
        # 昨日收盘涨幅% (昨收/前收-1)
        prev_pct = (prev_close / prev2_close - 1.0) * 100.0 \
            if (prev_close is not None and prev2_close and prev2_close > 0) else None
        # 昨日开盘涨幅% (昨开/前收-1) -- 昨开得不高(不虚高) 才入池
        prev_open_pct = (prev_open / prev2_close - 1.0) * 100.0 \
            if (prev_open is not None and prev2_close and prev2_close > 0) else None
        close_ser = g["close"].dropna()
        ma5_prev = float(close_ser.iloc[-6:-1].mean()) if len(close_ser) >= 6 else None
        ma10_prev = float(close_ser.iloc[-11:-1].mean()) if len(close_ser) >= 11 else None
        ma20_prev = float(close_ser.iloc[-21:-1].mean()) if len(close_ser) >= 21 else None
        above_ma: Dict[int, Optional[int]] = {}
        for period, ma in ((5, ma5_prev), (10, ma10_prev), (20, ma20_prev)):
            if prev_close is not None and ma is not None:
                above_ma[period] = 1 if prev_close > ma else 0
            else:
                above_ma[period] = None
        recs.append({
            "code": code,
            "date": str(last["trade_date"].date()),
            "prev_date": str(g.iloc[-2]["trade_date"].date()) if len(g) >= 2 else None,
            "prev2_date": str(g.iloc[-3]["trade_date"].date()) if len(g) >= 3 else None,
            "close": close,
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "vol": float(last["volume"]),
            "amount": float(last["amount"]) if last["amount"] is not None else None,
            "pct_change": pct,
            "vol_ratio": vol_ratio,
            # 弱转强相关列 (数据不足为 None -> 对应过滤自动跳过)
            "open_pct": open_pct,
            "prev_pct": prev_pct,
            "prev_open_pct": prev_open_pct,
            "vol_grow_pct": vol_grow_pct,
            "ma5_prev": ma5_prev,
            "ma10_prev": ma10_prev,
            "ma20_prev": ma20_prev,
            "above_ma5_prev": above_ma.get(5),
            "above_ma10_prev": above_ma.get(10),
            "above_ma20_prev": above_ma.get(20),
            # turnover/market_cap 需股本/市值元数据, 当前不可得 -> 不建列, 相关过滤自动跳过
        })
    meta = pd.DataFrame(recs).set_index("code")
    # 供信号类策略做一个简单可排序字段兜底
    if not meta.empty and "amount" in meta:
        meta["amount"] = meta["amount"].fillna(0.0)
    return UniverseSnapshot(meta, latest_date,
                            index_open_pct=_cached_index_open_pct(asof_date),
                            index_pct=_cached_index_pct(asof_date),
                            index_above_ma=_index_above_ma_map(asof_date),
                            index_prev_amount=_prev_amount)


def _stock_name_map() -> Dict[str, str]:
    """从 trade_stock_basic 批量取股票名称 (若表存在), 缺失返回空"""
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    out: Dict[str, str] = {}
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES LIKE 'trade_stock_basic'")
        if cur.fetchone():
            cur.execute("SELECT stock_code, stock_name FROM trade_stock_basic")
            for code, name in cur.fetchall():
                if code and name:
                    out[str(code)] = str(name)
        cur.close()
    except Exception:
        pass
    finally:
        conn.close()
    return out


def run_strategy_selection(inst: Dict[str, Any], snapshot: UniverseSnapshot,
                           capital: float) -> List[dict]:
    """按策略实例选股, 返回候选列表 [{strategy, stock_code, rank_no, score, reason, name}]"""
    name = inst.get("name")
    mode = (inst.get("selection") or {}).get("mode", "generic_filter")
    filters = (inst.get("selection") or {}).get("filters", {}) or {}
    top_n = int(inst.get("top_n") or 0)
    label = inst.get("label", name)
    name_map = _stock_name_map()

    def _reason_parts(r: pd.Series) -> list:
        """把"已启用"筛选参数对应的实际数值拼进理由, 多个条件逐项显示.
        每个指标标注其对应日期: 今日/上证指数用当日(date), 昨收用昨日(prev_date),
        昨量较前日用前日→昨日(prev2_date→prev_date)."""
        def _d(col):
            v = r.get(col)
            try:
                return pd.Timestamp(v).strftime("%m-%d")
            except Exception:
                return ""
        parts: List[str] = []
        # 弱转强: 按用户要求, 入选理由仅保留 昨收/昨开/今开 三项
        _weak = (name == "weak_to_strong")
        # 百分比类条件: (filter键, 显示名, 指标列, 日期列)
        for k, dis, col, dcol in (
            ("max_prev_pct", "昨收涨幅", "prev_pct", "prev_date"),
            ("max_prev_open_pct", "昨日开盘涨幅", "prev_open_pct", "prev_date"),
            ("min_open_pct", "今日开盘涨幅", "open_pct", "date"),
            ("min_vol_grow_pct", "昨量较前日", "vol_grow_pct", "prev2_date"),
        ):
            # 弱转强不做"昨量较前日"理由
            if _weak and k == "min_vol_grow_pct":
                continue
            if k not in filters or filters.get(k) is None:
                continue
            v = r.get(col)
            if pd.isna(v):
                continue
            d = _d(dcol)
            if k == "min_vol_grow_pct" and d:
                d = f"{d}→{_d('prev_date')}"
            parts.append(f"{dis}({d}){float(v):.2f}%" if d else f"{dis}{float(v):.2f}%")
        # 均线周期: 站上 N 日线
        period = filters.get("ma_period")
        if not _weak and period not in (None, "", 0):
            col = f"above_ma{int(period)}"
            v = r.get(col)
            if not pd.isna(v):
                parts.append(f"站上{int(period)}日线" if int(v) else f"未站上{int(period)}日线")
        # 大盘指数闸门 (上证指数当日开盘涨幅)
        if not _weak and filters.get("max_index_pct") not in (None, ""):
            ip = snapshot.index_open_pct
            if ip is not None:
                d = _d("date")
                parts.append(f"上证开盘涨幅({d}){ip:+.2f}%" if d else f"上证开盘涨幅{ip:+.2f}%")
        # 大盘指数昨日收盘站上 N 日线
        imp = filters.get("index_ma_period")
        if not _weak and imp not in (None, "", 0):
            flag = (snapshot.index_above_ma or {}).get(int(imp))
            if flag is not None:
                parts.append(f"昨日大盘站上{int(imp)}日线" if flag else f"昨日大盘未站上{int(imp)}日线")
        return parts

    def _rows_for_df(df: pd.DataFrame, sort_by: str = "score",
                     sort_desc: bool = True) -> List[dict]:
        rows: List[dict] = []
        if df is None or df.empty:
            return rows
        wdf = df.dropna(subset=["close"]) if "close" in df else df
        if wdf.empty:
            return rows
        sort_col = "amount" if sort_by not in wdf.columns else sort_by
        wdf = wdf.copy()
        if sort_col in wdf:
            wdf["_tmp_sort"] = pd.to_numeric(wdf[sort_col], errors="coerce").fillna(0)
        else:
            wdf["_tmp_sort"] = 0
        wdf = wdf.sort_values("_tmp_sort", ascending=not sort_desc)
        if top_n > 0:
            wdf = wdf.head(top_n)
        rank = 1
        for code, r in wdf.iterrows():
            parts = _reason_parts(r)
            reason = (f"{label}入选 · " + " · ".join(parts)) if parts else f"{label}条件入选(rank{rank})"
            rows.append({
                "strategy": name,
                "stock_code": str(code),
                "rank_no": rank,
                "score": float(r.get("score")) if pd.notna(r.get("score")) else None,
                "reason": reason,
                "name": name_map.get(str(code), ""),
                "open_pct": float(r.get("open_pct")) if pd.notna(r.get("open_pct")) else None,
                "cur_pct": float(r.get("pct_change")) if pd.notna(r.get("pct_change")) else None,
            })
            rank += 1
        return rows

    universe = inst.get("universe", "all_a")

    if mode == "code_list":
        codes = [str(c).strip() for c in (inst.get("code_list") or []) if str(c).strip()]
        sub = snapshot.meta.reindex(codes)
        return _rows_for_df(sub, sort_by=(inst.get("selection") or {}).get("sort_by", "amount"),
                            sort_desc=(inst.get("selection") or {}).get("sort_desc", True))

    if mode == "registry_scan":
        meta = get_strategy(name)
        selector = getattr(meta, "selector", None) if meta else None
        if selector is not None:
            meta_df = snapshot.meta.copy()
            try:
                scores = selector(meta_df, snapshot, capital)
            except Exception as e:
                scores = None
                print(f"[WARN] 策略 {name} selector 异常: {e}", flush=True)
            if scores is not None:
                meta_df["score"] = pd.to_numeric(scores, errors="coerce")
                meta_df = snapshot.filtered_df(filters).reindex(meta_df.index)
                meta_df["score"] = pd.to_numeric(scores.reindex(meta_df.index), errors="coerce")
                sort_by = (inst.get("selection") or {}).get("sort_by", "score")
                return _rows_for_df(meta_df, sort_by=sort_by,
                                    sort_desc=(inst.get("selection") or {}).get("sort_desc", True))
        # 无 selector 时退回 generic_filter
        mode = "generic_filter"

    if mode == "generic_filter":
        df = snapshot.filtered_df(filters)
        sort_by = (inst.get("selection") or {}).get("sort_by", "amount")
        return _rows_for_df(df, sort_by=sort_by,
                            sort_desc=(inst.get("selection") or {}).get("sort_desc", True))

    # unknown mode -> 空
    return []


def iter_strategy_signal_targets() -> List[Tuple[str, str]]:
    """返回 [(strategy, stock_code), ...]: 每个策略自己的候选池 + 持仓并集.

    供实盘循环按策略独立评估买卖 (互不影响, 无 default 兜底).
    """
    from lib.strategy_runner import enabled_strategy_instances
    from lib.selection_store import (
        query_selection_pool, query_strategy_positions,
    )
    out: List[Tuple[str, str]] = []
    for inst in enabled_strategy_instances():
        name = inst.get("name")
        if not name:
            continue
        seen: set = set()
        for r in query_selection_pool(strategy=name):
            c = str(r.get("stock_code", "")).strip()
            if c and c not in seen:
                seen.add(c)
                out.append((name, c))
        for p in query_strategy_positions(strategy=name):
            c = str(p.get("stock_code", "")).strip()
            if c and c not in seen:
                seen.add(c)
                out.append((name, c))
    return out


def clear_snapshot_cache() -> None:
    _clear_panel()