# -*- coding: utf-8 -*-
# 25-AI量化系统 回测引擎 -- 复用本项目的策略 evaluator，不引入 backtrader
"""
backtest_engine -- 在历史日 K 上回放已注册策略，算信号 + 撮合 + 指标

核心理念:
    策略 evaluator 签名是 (code, market, capital) -> {side, strategy, reason}.
    那么只要造一个"按时间截断的历史回放 market", 就能把策略代码原样跑在历史数据上,
    不必把策略再用 backtrader 重写一遍 (策略代码统一是关键).

撮合规则 (教学级):
    - 信号在 K_i 收盘出 -> 用 K_{i+1} 开盘价成交 (避免未来函数)
    - A 股 T+1: 当日买入次日才能卖
    - 仓位: position_pct% 资金满仓单只 (默认 95%)
    - 手续费: commission (单边) -- 简化, 实际可加印花税
    - 整手: 取整到 100 股的整数倍 (沪深 A 股最小单位)

注意:
    - 5 分钟周期策略在回测时只能拿到日 K,
      它们内部 _safe_kline(period='5m') 会返回 None -> 策略自动 hold, 不会假信号.
      想真做 5 分钟回测要接 xtdata 1m/5m, 后续扩展.
    - 仅做单股回测; 多股组合回测可后续扩展.
"""

from __future__ import annotations
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from lib.backtest_data import load_daily_kline, get_stock_name
from lib.backtest_metrics import compute_metrics
from lib.strategy_registry import get_strategy


# ============================================================
# ReplayMarket: 模拟 strategy evaluator 所需的 market 接口
# ============================================================

class ReplayMarket:
    """历史回放版 market

    策略 evaluator 调用的接口:
        market.get_recent_kline(code, period='1d'/'5m', count=N) -> DataFrame
            返回 close/high/low/open/volume 等列, 按时间升序

    策略一般只看最后 N 根 K, 所以这里 cur_idx 之前的全部历史都返回.
    period='5m' 时返回 None (回测里没有分钟数据 -- 让策略自然 hold).
    """

    def __init__(self, code: str, daily_df: pd.DataFrame):
        self.code = code
        self._df = daily_df.copy()
        self._df.sort_index(inplace=True)
        self.cur_idx = 0   # 当前可见的最后一根 K (含)

    def advance(self, idx: int):
        """把"当前时间"挪到第 idx 根 K (含)"""
        self.cur_idx = idx

    def get_recent_kline(self, stock_code: str,
                         period: str = "1d",
                         count: int = 50):
        """策略调这里. 仅支持 1d; 其他 period 返回 None 让策略 hold."""
        if stock_code != self.code:
            # 多股策略 (multi_factor_top 单股版本不会触发) -> 不支持
            return None
        if period not in ("1d", "1day", "day", "daily"):
            # 5 分钟 / tick 在日 K 回测里没有, 让策略自动 hold
            return None
        end = self.cur_idx + 1   # 含当前根
        start = max(0, end - count)
        sub = self._df.iloc[start:end]
        if len(sub) == 0:
            return None
        return sub

    def get_latest_tick(self, stock_code: str) -> dict:
        """简化: 用当前 K 收盘价当 tick (策略基本不用)"""
        if self.cur_idx >= len(self._df):
            return {}
        row = self._df.iloc[self.cur_idx]
        return {"lastPrice": float(row["close"]),
                "open":      float(row["open"]),
                "high":      float(row["high"]),
                "low":       float(row["low"]),
                "volume":    float(row["volume"])}


# ============================================================
# 单策略回测: 跑信号 + 撮合 + 指标
# ============================================================

def _round_lot(volume: int) -> int:
    """A 股最小买卖单位 100 股"""
    return (volume // 100) * 100


def run_backtest(stock_code: str,
                 strategy_name: str,
                 start_date: str,
                 end_date: str,
                 initial_cash: Optional[float] = None,
                 commission: Optional[float] = None,
                 position_pct: Optional[int] = None,
                 warmup_bars: int = 60) -> Dict[str, Any]:
    """单股 + 单策略回测

    Args:
        stock_code:   '600519.SH'
        strategy_name: strategy_registry 中的注册名 (grid_classic / weak_to_strong / ...)
        start_date:   'YYYY-MM-DD' (撮合区间起点; warmup_bars 会向前多拿一些以便策略指标预热)
        end_date:     'YYYY-MM-DD'
        initial_cash: 初始资金, 默认读 .env BACKTEST_INITIAL_CASH (1,000,000)
        commission:   单边手续费率, 默认 0.0002 (万 2)
        position_pct: 单只满仓比例 %, 默认 95
        warmup_bars:  额外向前拉的 K 线数 (让 EMA/MACD 等指标先稳定下来), 默认 60

    Returns:
        {
            "ok":          True/False,
            "stock_code":  ...,
            "stock_name":  '贵州茅台',
            "strategy":    ...,
            "kline":       [{"date":"YYYY-MM-DD","open","high","low","close","volume"}, ...],  # 撮合区间的日 K
            "signals":     [{"date","side","reason"}, ...],   # 策略给的所有 buy/sell 信号 (不一定都成交)
            "trades":      [{"date","side":"buy"/"sell","price","size","pnl"}, ...],
            "navs":        [{"date","nav"}, ...],            # 每日净值 (现金 + 持仓市值)
            "metrics":     compute_metrics(...) 输出,
            "buy_hold":    {"total_return", "annual_return"} -- 同期基准
        }
    """
    # 默认值从 .env 读 (兼容老接口里直接传 None)
    initial_cash = float(initial_cash if initial_cash is not None
                         else os.environ.get("BACKTEST_INITIAL_CASH", 1_000_000))
    commission = float(commission if commission is not None
                       else os.environ.get("BACKTEST_COMMISSION", 0.0002))
    position_pct = int(position_pct if position_pct is not None
                       else os.environ.get("BACKTEST_POSITION_PCT", 95))

    meta = get_strategy(strategy_name)
    if meta is None:
        return {"ok": False, "message": f"未知策略: {strategy_name}"}

    # 拉数据: 多取 warmup_bars 在 start_date 前面, 让 MACD/EMA 等先暖一下
    try:
        # 直接拉 [start - 1 年, end]; 然后裁剪
        from datetime import datetime, timedelta
        sd = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        ed = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
        # warmup 用 1.5 年自然日 (够 60+ 根日 K)
        warmup_start = (sd - timedelta(days=int(warmup_bars * 1.7))).strftime("%Y-%m-%d") if sd else None
        full_df = load_daily_kline(stock_code,
                                    start_date=warmup_start or start_date,
                                    end_date=end_date)
    except Exception as e:
        return {"ok": False, "message": f"加载 {stock_code} 失败: {e}"}

    if full_df is None or len(full_df) < 30:
        return {"ok": False, "message": f"{stock_code} 数据不足 (<30 根)"}

    # 找撮合起点 idx (撮合区间内的 K)
    if sd is not None:
        bt_mask = full_df.index >= pd.Timestamp(sd)
    else:
        bt_mask = pd.Series(True, index=full_df.index)
    if ed is not None:
        bt_mask &= full_df.index <= pd.Timestamp(ed)
    bt_indices = [i for i, in_range in enumerate(bt_mask) if in_range]
    if not bt_indices:
        return {"ok": False, "message": f"撮合区间 {start_date}~{end_date} 内无数据"}

    market = ReplayMarket(stock_code, full_df)

    # ============== 撮合状态 ==============
    cash = initial_cash
    position = 0          # 当前持仓股数
    cost_basis = 0.0      # 持仓总成本 (按入场价 * 股数 + 手续费)
    last_buy_date = None  # T+1 防止当日买卖

    signals: List[Dict[str, Any]] = []
    trades:  List[Dict[str, Any]] = []
    navs:    List[Dict[str, Any]] = []

    pending_order: Optional[Dict[str, Any]] = None   # 上一根 K 信号 -> 这根 K 开盘成交

    bt_first_idx = bt_indices[0]
    bt_last_idx = bt_indices[-1]

    for i in range(bt_first_idx, bt_last_idx + 1):
        market.advance(i)
        bar = full_df.iloc[i]
        date_str = full_df.index[i].strftime("%Y-%m-%d")
        bar_open = float(bar["open"])
        bar_close = float(bar["close"])

        # ---- 1) 先撮合上一根产生的 pending order (本根开盘价) ----
        if pending_order is not None:
            side = pending_order["side"]
            if side == "buy" and position == 0:
                # 资金分配: position_pct% 资金 / 开盘价 -> 整手
                budget = cash * (position_pct / 100.0)
                raw_qty = int(budget // bar_open) if bar_open > 0 else 0
                qty = _round_lot(raw_qty)
                if qty >= 100:
                    cost = qty * bar_open
                    fee = cost * commission
                    if cash >= cost + fee:
                        cash -= (cost + fee)
                        position = qty
                        cost_basis = cost + fee
                        last_buy_date = full_df.index[i]
                        trades.append({
                            "date":  date_str,
                            "side":  "buy",
                            "price": round(bar_open, 4),
                            "size":  qty,
                            "pnl":   0.0,
                            "reason": pending_order.get("reason", ""),
                        })
            elif side == "sell" and position > 0:
                # T+1: 当日买的次日才能卖
                buy_today = (last_buy_date is not None
                             and last_buy_date == full_df.index[i])
                if not buy_today:
                    revenue = position * bar_open
                    fee = revenue * commission
                    cash += (revenue - fee)
                    pnl = revenue - fee - cost_basis
                    trades.append({
                        "date":  date_str,
                        "side":  "sell",
                        "price": round(bar_open, 4),
                        "size":  position,
                        "pnl":   round(pnl, 2),
                        "reason": pending_order.get("reason", ""),
                    })
                    position = 0
                    cost_basis = 0.0
            pending_order = None

        # ---- 2) 当日收盘后跑策略, 决定 pending_order ----
        try:
            sig = meta.evaluator(stock_code, market, initial_cash)
        except Exception as e:
            sig = {"side": "hold", "strategy": strategy_name,
                   "reason": f"策略异常 {type(e).__name__}: {e}"}
        side = sig.get("side", "hold")
        if side in ("buy", "sell"):
            signals.append({
                "date":   date_str,
                "side":   side,
                "reason": sig.get("reason", ""),
            })
            # T+1 防御: sell 当日如果是买入当日, 就推到下一根 (跟撮合期一致, 不另外标记)
            pending_order = {"side": side, "reason": sig.get("reason", "")}

        # ---- 3) 记录当日净值 (现金 + 持仓 * 收盘) ----
        navs.append({
            "date": date_str,
            "nav":  round(cash + position * bar_close, 2),
        })

    # 区间结束: 不平仓, 直接按最后一根收盘评估
    last_bar = full_df.iloc[bt_last_idx]
    final_nav = cash + position * float(last_bar["close"])
    if not navs:
        navs = [{"date": last_bar.name.strftime("%Y-%m-%d"), "nav": round(final_nav, 2)}]
    metrics = compute_metrics(initial_cash, trades, navs)

    # 同期基准 (买入持有 = 第一根开盘买, 最后一根收盘卖, 含手续费简化)
    bh_open = float(full_df.iloc[bt_first_idx]["open"])
    bh_close = float(full_df.iloc[bt_last_idx]["close"])
    bh_total = (bh_close - bh_open) / bh_open if bh_open > 0 else 0.0
    bh_years = max(metrics["years"], 1e-6)
    bh_annual = (1 + bh_total) ** (1 / bh_years) - 1 if bh_total > -1 else bh_total

    # 输出 K 线 (用于前端画图)
    bt_slice = full_df.iloc[bt_first_idx:bt_last_idx + 1]
    kline_out = [
        {
            "date":   d.strftime("%Y-%m-%d"),
            "open":   round(float(r["open"]), 4),
            "high":   round(float(r["high"]), 4),
            "low":    round(float(r["low"]), 4),
            "close":  round(float(r["close"]), 4),
            "volume": int(r["volume"]) if not pd.isna(r["volume"]) else 0,
        }
        for d, r in bt_slice.iterrows()
    ]

    return {
        "ok":         True,
        "stock_code": stock_code,
        "stock_name": get_stock_name(stock_code),
        "strategy":   strategy_name,
        "strategy_label": meta.label,
        "params": {
            "start_date":  start_date,
            "end_date":    end_date,
            "initial_cash": initial_cash,
            "commission":  commission,
            "position_pct": position_pct,
        },
        "kline":      kline_out,
        "signals":    signals,
        "trades":     trades,
        "navs":       navs,
        "metrics":    metrics,
        "buy_hold": {
            "total_return":  round(bh_total,  6),
            "annual_return": round(bh_annual, 6),
        },
    }
