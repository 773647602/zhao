# -*- coding: utf-8 -*-
# 24-CASE-B: Walk-Forward Analysis (前进式验证 / 过拟合检测)
"""
WalkForwardAnalysis -- 滚动窗口验证 + 过拟合警报

学员痛点: "回测年化 50%, 实盘亏 20%. 怎么提前发现策略过拟合?"
答: Walk-Forward (前进式验证)

核心思想:
    传统回测 = "用 2024 全年数据找最优参数 -> 看 2024 全年表现"
        问题: 既用数据找参数, 又用同一份数据评估 -- 必过拟合
    
    Walk-Forward = "用过去 N 个月找参数 -> 用接下来 M 个月评估 -> 滚动"
        优点: 训练和评估完全分离, 暴露策略真实表现

流程示意:
    时间轴: |---训练 60d---|--评估 20d--|---训练 60d---|--评估 20d--|...
            ^                          ^
            t0                         t0 + 80d (移动一个评估窗口)

输出指标:
    - 每窗口的"训练最优参数"
    - 每窗口的"训练 IS 表现 vs 评估 OOS 表现"
    - IS / OOS 比例 -- 接近 1.0 = 鲁棒, 远小于 1.0 = 过拟合
    - 过拟合分数 = (IS_sharpe - OOS_sharpe) / IS_sharpe
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Callable, List, Optional
import numpy as np
import pandas as pd


@dataclass
class WalkForwardWindow:
    window_id:    int
    train_start:  str
    train_end:    str
    test_start:   str
    test_end:     str
    best_params:  dict
    train_sharpe: float
    test_sharpe:  float
    train_ret:    float    # 训练期总收益
    test_ret:     float    # 评估期总收益


@dataclass
class WalkForwardReport:
    windows:           List[WalkForwardWindow]
    avg_train_sharpe:  float
    avg_test_sharpe:   float
    is_oos_ratio:      float    # OOS / IS 比例 (越接近 1 越好)
    overfit_score:     float    # (IS - OOS) / IS
    by_window:         pd.DataFrame

    def __str__(self):
        verdict = ("无明显过拟合" if self.overfit_score < 0.3
                   else "存在过拟合" if self.overfit_score < 0.7
                   else "严重过拟合")
        return (f"窗口数        : {len(self.windows)}\n"
                f"平均 IS Sharpe : {self.avg_train_sharpe:>+.3f}\n"
                f"平均 OOS Sharpe: {self.avg_test_sharpe:>+.3f}\n"
                f"OOS/IS 比例    : {self.is_oos_ratio:.2f}  (1.0 完美, < 0.5 过拟合)\n"
                f"过拟合分数     : {self.overfit_score:>+.2%}  -> {verdict}")


# ============================================================
# Walk-Forward 主流程
# ============================================================

def walk_forward_analysis(
    df: pd.DataFrame,                 # 含 close 列, 升序日期
    strategy_fn: Callable,            # strategy_fn(df, params) -> trades_list
    param_grid: List[dict],           # 候选参数列表 (e.g. [{"ma_short": 5, "ma_long": 20}, ...])
    train_window: int = 60,           # 训练窗口大小 (交易日)
    test_window: int = 20,            # 评估窗口大小
    risk_free: float = 0.025,
    oos_warmup: int = 0,              # 评估阶段额外回看的 warmup 行数 (ML 策略需要)
) -> WalkForwardReport:
    """滚动 walk-forward

    每个窗口:
        1. 用 train_window 数据跑 param_grid 里所有参数, 选 Sharpe 最高的
        2. 用 best_params 在 test_window 上重新跑, 算 OOS Sharpe
        3. 滚动 test_window 步长

    oos_warmup: ML 类策略 (内部含滚动训练, 如 ml_prob) 在 test_window 行上无法启动
                -- 它需要回看 N 行作为 warmup 才能开始预测. 设了 oos_warmup>0 后,
                评估阶段会把 [cursor+train_window-warmup : cursor+train_window+test_window]
                整段喂给策略, 然后只保留 entry_idx 落在 test 段内的交易.
                简单技术策略 (双均线 / 布林带 / RSI / 唐奇安) 留 0 即可.
    """
    n = len(df)
    if n < train_window + test_window:
        raise ValueError(f"数据不足, 至少需要 {train_window + test_window} 行")

    windows = []
    window_id = 0

    cursor = 0
    while cursor + train_window + test_window <= n:
        train_df = df.iloc[cursor: cursor + train_window]
        test_start = cursor + train_window
        test_end = cursor + train_window + test_window
        test_df = df.iloc[test_start: test_end]

        # 1) 在训练集上找最优参数
        # 评分: (Sharpe, Total_Return, -Trade_Count) -- Sharpe 优先, 收益其次, 交易少打平
        best_params = None
        best_score = (-np.inf, -np.inf)
        best_train_sharpe = 0
        best_train_ret = 0
        for params in param_grid:
            trades = strategy_fn(train_df, params)
            sharpe, ret = _sharpe_and_ret(trades, train_df, risk_free)
            score = (sharpe, ret)
            if score > best_score:
                best_score = score
                best_train_sharpe = sharpe
                best_train_ret = ret
                best_params = params
        if best_params is None:
            best_params = param_grid[0]

        # 2) 在评估集上跑 best_params
        if oos_warmup > 0:
            # 含 warmup 的评估: 给策略一段 [warmup_start : test_end] 数据, 内部能 fit + predict;
            # 跑完后只保留 entry_idx 落在 test 段内的 trades, 并把 idx 平移到相对 test_df 的位置
            warmup_start = max(0, test_start - oos_warmup)
            eval_df = df.iloc[warmup_start: test_end]
            full_trades = strategy_fn(eval_df, best_params)
            test_offset = test_start - warmup_start  # test 段在 eval_df 中的起始 idx
            oos_trades = []
            for t in full_trades:
                ei = t.get("entry_idx", -1)
                xi = t.get("exit_idx", -1)
                if ei >= test_offset:
                    oos_trades.append({
                        **t,
                        "entry_idx": ei - test_offset,
                        "exit_idx":  xi - test_offset,
                    })
            test_sharpe, test_ret = _sharpe_and_ret(oos_trades, test_df, risk_free)
        else:
            oos_trades = strategy_fn(test_df, best_params)
            test_sharpe, test_ret = _sharpe_and_ret(oos_trades, test_df, risk_free)

        windows.append(WalkForwardWindow(
            window_id=    window_id,
            train_start=  str(train_df.index[0])[:10],
            train_end=    str(train_df.index[-1])[:10],
            test_start=   str(test_df.index[0])[:10],
            test_end=     str(test_df.index[-1])[:10],
            best_params=  best_params,
            train_sharpe= best_train_sharpe,
            test_sharpe=  test_sharpe,
            train_ret=    best_train_ret,
            test_ret=     test_ret,
        ))
        window_id += 1
        cursor += test_window

    avg_train = np.mean([w.train_sharpe for w in windows])
    avg_test = np.mean([w.test_sharpe for w in windows])
    is_oos_ratio = avg_test / avg_train if avg_train > 0 else 0
    overfit = (avg_train - avg_test) / avg_train if avg_train > 0 else 0

    by_window_df = pd.DataFrame([
        {"window": w.window_id, "train": f"{w.train_start}~{w.train_end}",
         "test": f"{w.test_start}~{w.test_end}",
         "best_params": str(w.best_params),
         "train_sharpe": round(w.train_sharpe, 2),
         "test_sharpe": round(w.test_sharpe, 2),
         "train_ret": f"{w.train_ret:.2%}",
         "test_ret": f"{w.test_ret:.2%}"}
        for w in windows
    ])

    return WalkForwardReport(
        windows=windows,
        avg_train_sharpe=avg_train,
        avg_test_sharpe=avg_test,
        is_oos_ratio=is_oos_ratio,
        overfit_score=overfit,
        by_window=by_window_df,
    )


def _sharpe_and_ret(trades: list, df: pd.DataFrame, risk_free: float = 0.025) -> tuple:
    """从交易记录算 Sharpe + 累计收益.

    口径 (业内主流, 跟 pyfolio / quantstats / 21章成本模型一致):
        - 把 trades 还原成评估期内的"逐日收益序列": 持仓日 = close 日涨跌, 空仓日 = 0
        - Sharpe 用标准日收益年化公式: mean*250 / (std*sqrt(250))
        - 累计收益用 (1 + daily_rets) 累乘 - 1 (复利, 而不是单笔收益简单求和)
        - 这种口径下 Sharpe 跟 buy-and-hold 可比, 跟"单笔交易胜率"等口径不混淆

    Args:
        trades: [{"entry_idx": int, "exit_idx": int, "return_pct": float}, ...]
                entry_idx / exit_idx 都是相对 df 的 iloc 位置 (闭区间, exit 是平仓那一根 K)
        df:     评估期 K 线 (含 close 列, 升序)
        risk_free: 年化无风险利率 (默认 2.5%)

    Returns:
        (sharpe_annualized, total_return_compound)
    """
    close = df["close"].astype(float).values
    n = len(close)
    if not trades or n < 2:
        return 0.0, 0.0

    # 构造逐日收益序列: 持仓期 [entry+1, exit] 用 close pct change, 空仓期 = 0
    # entry 当天用 entry_price 假设买入, 收益从次日起算; exit 当天平仓, 算平仓那段日收益
    daily_rets = np.zeros(n)
    for t in trades:
        ei = t.get("entry_idx")
        xi = t.get("exit_idx")
        if ei is None or xi is None or ei < 0 or xi <= ei or xi >= n:
            continue
        for i in range(ei + 1, xi + 1):
            if close[i - 1] > 0:
                daily_rets[i] = close[i] / close[i - 1] - 1.0

    # 累计收益 (复利)
    total_ret = float(np.prod(1.0 + daily_rets) - 1.0)

    # 至少 2 个非零日收益才能算波动率
    nonzero_days = int((daily_rets != 0).sum())
    if nonzero_days < 2:
        return 0.0, total_ret
    annual_ret = float(daily_rets.mean() * 250)
    annual_vol = float(daily_rets.std(ddof=1) * math.sqrt(250))
    if annual_vol == 0:
        return 0.0, total_ret
    sharpe = (annual_ret - risk_free) / annual_vol
    return float(sharpe), total_ret


# ============================================================
# 内置策略 (供 walk-forward 使用)
# ============================================================
# 所有策略签名统一: strategy_fn(df, params) -> trades = [{"return_pct": float}, ...]
# df: 含 close 列, 升序日期
# return_pct: 单次交易百分比收益, 单位 % (如 5.0 表示 +5%)
# ============================================================


def double_ma_strategy(df: pd.DataFrame, params: dict) -> list:
    """双均线趋势跟踪 -- 短均线上穿长均线买入, 下穿卖出.
    params: {ma_short, ma_long}; 适合趋势行情, 横盘 N 字过拟合"""
    short = params.get("ma_short", 5)
    long_ = params.get("ma_long", 20)
    if len(df) < long_ + 1:
        return []
    close = df["close"].astype(float).values
    ma_s = pd.Series(close).rolling(short).mean().values
    ma_l = pd.Series(close).rolling(long_).mean().values

    trades = []
    in_pos = False
    entry_price = 0
    entry_idx = -1
    for i in range(long_, len(close)):
        prev_diff = ma_s[i - 1] - ma_l[i - 1]
        curr_diff = ma_s[i] - ma_l[i]
        if prev_diff <= 0 and curr_diff > 0 and not in_pos:
            in_pos = True
            entry_price = close[i]
            entry_idx = i
        elif prev_diff >= 0 and curr_diff < 0 and in_pos:
            ret = (close[i] - entry_price) / entry_price * 100
            trades.append({"entry_idx": entry_idx, "exit_idx": i, "return_pct": ret})
            in_pos = False
    return trades


def bollinger_reversion_strategy(df: pd.DataFrame, params: dict) -> list:
    """布林带均值回归 -- 收盘价触下轨买入 (超卖), 触上轨卖出 (超买).
    params: {window, std_n}; 适合震荡, 单边趋势会持续被反向打脸"""
    window = int(params.get("window", 20))
    std_n = float(params.get("std_n", 2.0))
    if len(df) < window + 1:
        return []
    close = df["close"].astype(float).values
    s = pd.Series(close)
    mid = s.rolling(window).mean().values
    std = s.rolling(window).std(ddof=0).values
    upper = mid + std_n * std
    lower = mid - std_n * std

    trades = []
    in_pos = False
    entry_price = 0.0
    entry_idx = -1
    for i in range(window, len(close)):
        if not in_pos and close[i] <= lower[i]:
            in_pos = True
            entry_price = close[i]
            entry_idx = i
        elif in_pos and close[i] >= upper[i]:
            ret = (close[i] - entry_price) / entry_price * 100
            trades.append({"entry_idx": entry_idx, "exit_idx": i, "return_pct": ret})
            in_pos = False
    return trades


def rsi_reversion_strategy(df: pd.DataFrame, params: dict) -> list:
    """RSI 反转 -- RSI 跌破 oversold 买入, 涨破 overbought 卖出.
    params: {rsi_period, oversold, overbought}; 经典超买超卖, 强趋势失效"""
    n = int(params.get("rsi_period", 14))
    os = float(params.get("oversold", 30))
    ob = float(params.get("overbought", 70))
    if len(df) < n + 2:
        return []
    close = df["close"].astype(float).values
    s = pd.Series(close)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    # Wilder 平滑 (EWM, alpha=1/n) 是 RSI 业界主流
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi_v = rsi.values

    trades = []
    in_pos = False
    entry_price = 0.0
    entry_idx = -1
    for i in range(n + 1, len(close)):
        r = rsi_v[i]
        if np.isnan(r):
            continue
        if not in_pos and r < os:
            in_pos = True
            entry_price = close[i]
            entry_idx = i
        elif in_pos and r > ob:
            ret = (close[i] - entry_price) / entry_price * 100
            trades.append({"entry_idx": entry_idx, "exit_idx": i, "return_pct": ret})
            in_pos = False
    return trades


def ml_prob_strategy(df: pd.DataFrame, params: dict) -> list:
    """ML 概率因子 -- 50+ 技术因子 + XGBoost/LightGBM 滚动训练, 输出涨概率, 阈值出入场.
    params: {train_days, retrain_interval, buy_th, sell_th [, horizon, model_type]}

    与简单技术策略 (双均线 / 布林带 / RSI / 唐奇安) 的根本区别:
        - 滚动重训本身就是 walk-forward 思想内置, OOS 衰减通常明显小
        - 50+ 因子 + 树模型, 不依赖单一指标
        - 训练样本量是关键: train_days 太小或数据总量不足会让 ML 退化成噪声

    实现委托给 ml_strategy.ml_prob_runner.run_ml_prob (实盘/回测/WF 共用同一函数,
    保证 WF 上的过拟合诊断与实盘信号口径一致).
    """
    from ml_strategy.ml_prob_runner import run_ml_prob

    out = run_ml_prob(
        df,
        train_days=int(params.get("train_days", 120)),
        retrain_interval=int(params.get("retrain_interval", 20)),
        buy_th=float(params.get("buy_th", 0.6)),
        sell_th=float(params.get("sell_th", 0.4)),
        horizon=int(params.get("horizon", 1)),
        model_type=str(params.get("model_type", "xgboost")),
        code=None,             # WF 训练/评估窗口不同 df, 不能复用缓存
        verbose=False,
    )
    return out["trades"]


def donchian_breakout_strategy(df: pd.DataFrame, params: dict) -> list:
    """唐奇安通道突破 (海龟交易经典) -- 收盘突破 entry_lookback 日新高买入,
    跌破 exit_lookback 日新低卖出.
    params: {entry_lookback, exit_lookback}; 趋势好, 横盘连续假突破"""
    entry_n = int(params.get("entry_lookback", 20))
    exit_n = int(params.get("exit_lookback", 10))
    warmup = max(entry_n, exit_n)
    if len(df) < warmup + 1:
        return []
    close = df["close"].astype(float).values
    s = pd.Series(close)
    # i 当日突破判定基于 [i-N, i-1] 的极值, 不含当日, 避免未来函数
    hi = s.shift(1).rolling(entry_n).max().values
    lo = s.shift(1).rolling(exit_n).min().values

    trades = []
    in_pos = False
    entry_price = 0.0
    entry_idx = -1
    for i in range(warmup, len(close)):
        if np.isnan(hi[i]) or np.isnan(lo[i]):
            continue
        if not in_pos and close[i] > hi[i]:
            in_pos = True
            entry_price = close[i]
            entry_idx = i
        elif in_pos and close[i] < lo[i]:
            ret = (close[i] - entry_price) / entry_price * 100
            trades.append({"entry_idx": entry_idx, "exit_idx": i, "return_pct": ret})
            in_pos = False
    return trades


# ============================================================
# CLI
# ============================================================

def main():
    """完整 demo: 拉茅台历史数据 + walk-forward 过拟合检测"""
    import argparse
    from xtquant import xtdata

    parser = argparse.ArgumentParser(description="Walk-Forward 过拟合检测")
    parser.add_argument("--code", default="600519.SH")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--train", type=int, default=60)
    parser.add_argument("--test", type=int, default=20)
    args = parser.parse_args()

    xtdata.connect()
    xtdata.download_history_data(args.code, period="1d", start_time="20230101")
    data = xtdata.get_market_data_ex(
        field_list=["close"], stock_list=[args.code], period="1d",
        count=args.count, dividend_type="back",
    )
    df = data[args.code].copy()
    df.index = pd.to_datetime(df.index)

    print(f"\n{'='*78}")
    print(f"  CASE-24B Walk-Forward 过拟合检测 demo")
    print(f"  标的: {args.code}, 数据 {len(df)} 行, 训练 {args.train}d / 评估 {args.test}d")
    print(f"{'='*78}\n")

    # 候选参数: 6 组双均线
    param_grid = [
        {"ma_short": 5,  "ma_long": 20},
        {"ma_short": 5,  "ma_long": 30},
        {"ma_short": 10, "ma_long": 30},
        {"ma_short": 10, "ma_long": 60},
        {"ma_short": 20, "ma_long": 60},
        {"ma_short": 20, "ma_long": 120},
    ]

    print(f"[1] Walk-Forward Analysis (6 个参数候选, 每窗口选最优)")
    report = walk_forward_analysis(
        df, double_ma_strategy, param_grid,
        train_window=args.train, test_window=args.test,
    )

    print(f"\n[各窗口明细]")
    print(report.by_window.to_string(index=False))

    print(f"\n[汇总]\n{report}")

    # 给出投资建议
    print(f"\n[投资建议]")
    if report.overfit_score < 0.3:
        print("  策略相对鲁棒, 可以考虑实盘 (但仍需 dry-run 验证)")
    elif report.overfit_score < 0.7:
        print("  存在中度过拟合, 建议:")
        print("    1) 减小参数空间")
        print("    2) 加入交易成本 (复用 21章 trading_cost)")
        print("    3) 降低仓位 (复用 21章 position_sizer)")
    else:
        print("  严重过拟合, 不应实盘! 重新设计策略")


if __name__ == "__main__":
    main()
