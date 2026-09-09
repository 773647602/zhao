# -*- coding: utf-8 -*-
# 25-AI量化系统 策略注册中心 -- 将多类策略统一封装为 (code, market, capital) -> signal
"""
StrategyRegistry -- 策略注册中心 (单只股票视角)

设计理念:
    - live_loop 每轮对 watch 池的每只股票调一次 evaluator
    - 我们这里把"全市场扫描"型策略 (多因子/龙头) 也包装成"单只股票评估"模式
    - 每个策略都返回统一格式: {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str}

技术指标里 MACD 有两种 (名称里写清周期, 避免和日线混淆):

        - macd_5min      5 分钟 K 线, 参数 12/26/9 (快线/慢线指「根数」为 5 分钟 bar)
        - macd_1d        日 K 线, 参数 12/26/9 (经典「日线 MACD」)

    [技术指标]
        - macd_5min      5min K 线 MACD (日内短线)
        - macd_1d        日 K 线 MACD (波段)
        - dual_ma_5min   5min K 线 5/20 EMA 双均线
        - ma20_hold      日 K 收盘突破 MA20 买入, 跌破 MA20 卖出

    [量化选股]
        - multi_factor_top   多因子轻量版 (MOM_1M + RSI + BIAS_20 三因子合成)

    [龙头动量]
        - dragon_picker  当日涨幅 + 量比 + 价位 计算龙头分

    [震荡网格]
        - grid_classic   过去 60 日 high/low 切 8 格的经典网格

每个策略都做了健壮兜底: 数据不足 / 异常 -> 返回 hold (不会让循环挂掉)
"""

from __future__ import annotations
import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# 数据类
# ============================================================

@dataclass
class ParamSpec:
    """策略可调参数定义 (缺省值即代码里原本硬编码的那个常量)"""
    key: str                          # 参数名 (evaluator 里用 P() 取)
    label: str                        # 中文显示名
    default: Any                      # 缺省值
    ptype: str = "float"              # int / float / bool
    min: Optional[float] = None       # 前端输入下限
    max: Optional[float] = None       # 前端输入上限
    step: Optional[float] = None      # 前端步进
    desc: str = ""                    # 说明 (悬浮提示)


def _p_int(key, label, default, *, min=None, max=None, step=1, desc=""):
    return ParamSpec(key, label, default, "int", min, max, step, desc)


def _p_float(key, label, default, *, min=None, max=None, step=0.01, desc=""):
    return ParamSpec(key, label, default, "float", min, max, step, desc)


def _p_bool(key, label, default, desc=""):
    return ParamSpec(key, label, default, "bool", None, None, None, desc)


@dataclass
class StrategyMeta:
    """策略元信息"""
    name: str                      # 唯一标识 (路由表里用)
    label: str                     # 中文显示名
    group: str                     # 分组 (技术指标 / 量化选股 / 龙头动量 / 震荡网格)
    description: str             # 一句话说明 (列表/折叠区仍用)
    evaluator: Callable            # (code, market, capital) -> dict
    scenario: str = ""             # 适用场景 (弹窗)
    rules: str = ""              # 规则要点 (弹窗)
    example: str = ""              # 简短示例 (弹窗)
    params: List[ParamSpec] = field(default_factory=list)   # 可调参数定义
    selector: Optional[Callable] = None  # 可选: 全市场选股器 (snapshot_meta, market, capital) -> score Series


# ============================================================
# 注册中心
# ============================================================

_REGISTRY: Dict[str, StrategyMeta] = {}

# 参数覆盖值 (用户在前端改过的): {strategy_name: {param_key: value}}
_PARAM_OVERRIDES: Dict[str, Dict[str, Any]] = {}
_PARAM_LOCK = threading.Lock()

# 参数持久化文件
PARAMS_FILE = Path(__file__).resolve().parent.parent / "config" / "strategy_params.json"


def register(
    name: str,
    label: str,
    group: str,
    description: str = "",
    *,
    scenario: str = "",
    rules: str = "",
    example: str = "",
    params: Optional[List[ParamSpec]] = None,
    selector: Optional[Callable] = None,
):
    """装饰器: 注册一个策略 (scenario / rules / example 供持仓说明弹窗结构化展示)"""
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = StrategyMeta(
            name=name,
            label=label,
            group=group,
            description=description,
            evaluator=fn,
            scenario=scenario,
            rules=rules,
            example=example,
            params=list(params or []),
            selector=selector,
        )
        return fn
    return deco


def get_strategy(name: str) -> Optional[StrategyMeta]:
    return _REGISTRY.get(name)


def get_selector(name: str) -> Optional[Callable]:
    """取某策略的原生全市场选股器 (无则 None)"""
    meta = _REGISTRY.get(name)
    return getattr(meta, "selector", None) if meta else None


# ============================================================
# 参数读写 (缺省值 -> config/strategy_params.json 覆盖值)
# ============================================================

def load_param_overrides() -> Dict[str, Dict[str, Any]]:
    """从 config/strategy_params.json 读参数覆盖值 (进程内缓存)"""
    global _PARAM_OVERRIDES
    with _PARAM_LOCK:
        if _PARAM_OVERRIDES:
            return _PARAM_OVERRIDES
        out: Dict[str, Dict[str, Any]] = {}
        try:
            if PARAMS_FILE.exists():
                raw = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict):
                            out[str(k)] = dict(v)
        except Exception as e:
            print(f"[WARN] 读取 strategy_params.json 失败, 使用缺省参数: {e}", flush=True)
        _PARAM_OVERRIDES = out
        return _PARAM_OVERRIDES


def save_param_overrides(data: Dict[str, Dict[str, Any]]) -> None:
    """写参数覆盖值到文件并刷新进程内缓存"""
    global _PARAM_OVERRIDES
    with _PARAM_LOCK:
        PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PARAMS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _PARAM_OVERRIDES = {k: dict(v) for k, v in data.items()}


def P(strategy: str, key: str) -> Any:
    """取策略参数生效值: 覆盖值优先, 否则用注册时的缺省值"""
    ov = _PARAM_OVERRIDES.get(strategy)
    if ov and key in ov:
        return ov[key]
    meta = _REGISTRY.get(strategy)
    if meta:
        for spec in meta.params:
            if spec.key == key:
                return spec.default
    raise KeyError(f"策略 {strategy} 无参数 {key}")


def get_params(name: str) -> List[Dict[str, Any]]:
    """返回某策略的参数清单 (含缺省值 + 当前生效值), 供前端渲染表单"""
    load_param_overrides()
    meta = _REGISTRY.get(name)
    if meta is None:
        return []
    ov = _PARAM_OVERRIDES.get(name, {})
    out = []
    for s in meta.params:
        cur = ov.get(s.key, s.default)
        out.append({
            "key": s.key, "label": s.label, "type": s.ptype,
            "default": s.default, "current": cur,
            "min": s.min, "max": s.max, "step": s.step, "desc": s.desc,
            "modified": s.key in ov and ov[s.key] != s.default,
        })
    return out


def set_params(name: str, values: Dict[str, Any]) -> List[str]:
    """保存某策略的参数覆盖值; 只接受已定义的 key, 返回被忽略的 key 列表"""
    load_param_overrides()
    meta = _REGISTRY.get(name)
    if meta is None:
        raise KeyError(f"策略 {name} 未注册")
    specs = {s.key: s for s in meta.params}
    clean: Dict[str, Any] = {}
    ignored: List[str] = []
    for k, v in (values or {}).items():
        spec = specs.get(k)
        if spec is None:
            ignored.append(k)
            continue
        if spec.ptype == "bool":
            clean[k] = bool(v)
        elif spec.ptype == "int":
            clean[k] = int(float(v))
        else:
            clean[k] = float(v)
        if spec.min is not None and clean[k] < spec.min:
            clean[k] = spec.min
        if spec.max is not None and clean[k] > spec.max:
            clean[k] = spec.max
    # 与缺省值相同的项不写入覆盖表 (保持文件干净)
    defaults = {s.key: s.default for s in meta.params}
    cur = dict(_PARAM_OVERRIDES.get(name, {}))
    for k, v in clean.items():
        if v == defaults.get(k):
            cur.pop(k, None)
        else:
            cur[k] = v
    data = dict(_PARAM_OVERRIDES)
    if cur:
        data[name] = cur
    else:
        data.pop(name, None)
    save_param_overrides(data)
    return ignored


def list_strategies() -> List[Dict[str, str]]:
    """按分组返回所有可用策略 (供前端展示)"""
    out = []
    for meta in _REGISTRY.values():
        out.append({
            "name":        meta.name,
            "label":       meta.label,
            "group":       meta.group,
            "description": meta.description,
            "scenario":    meta.scenario,
            "rules":       meta.rules,
            "example":     meta.example,
            "params":      get_params(meta.name),
        })
    return out


def list_groups() -> Dict[str, List[Dict[str, str]]]:
    """按分组聚合: {group: [{name, label, desc}, ...]}"""
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for meta in _REGISTRY.values():
        grouped.setdefault(meta.group, []).append({
            "name":        meta.name,
            "label":       meta.label,
            "description": meta.description,
            "scenario":    meta.scenario,
            "rules":       meta.rules,
            "example":     meta.example,
        })
    return grouped


# ============================================================
# 通用工具: 拉 K 线 (容错)
# ============================================================

def _safe_kline(market, code: str, period: str, count: int):
    """拉 K 线, 拿不到返回 None"""
    try:
        df = market.get_recent_kline(code, period=period, count=count)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception:
        return None


def _hold(strategy: str, reason: str = "") -> dict:
    return {"side": "hold", "strategy": strategy, "reason": reason}


def _signal(side: str, strategy: str, reason: str = "") -> dict:
    return {"side": side, "strategy": strategy, "reason": reason}


def _macd_cross_from_close(close, strategy_name: str, bar_desc: str) -> dict:
    """
    经典 MACD: 快线=收盘 EMA12, 慢线=收盘 EMA26, DIF=快线-慢线, DEA=DIF 的 EMA9;
    信号: DIF 上穿 DEA 买, 下穿卖。
    bar_desc: 用于 reason 里标明周期, 如 "5min" / "日K"
    参数 (fast/slow/signal) 可通过前端「配置」调整, 缺省值 12/26/9。
    """
    fast = int(P(strategy_name, "fast"))
    slow = int(P(strategy_name, "slow"))
    signal = int(P(strategy_name, "signal"))
    need = slow + signal
    if close is None or len(close) < need:
        return _hold(strategy_name, f"K 线不足 {need} 根")
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    if len(dif) < 2:
        return _hold(strategy_name, "DIF 序列过短")
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    pset = f"{fast}/{slow}/{signal}"
    if prev <= 0 and curr > 0:
        return _signal("buy", strategy_name,
                       f"[{bar_desc}] 金叉 {pset} DIF-DEA={curr:+.4f}")
    if prev >= 0 and curr < 0:
        return _signal("sell", strategy_name,
                       f"[{bar_desc}] 死叉 {pset} DIF-DEA={curr:+.4f}")
    return _hold(strategy_name, f"[{bar_desc}] 无交叉")


# ============================================================
# 策略 1a: MACD 5min（日内短线，非日线）
# ============================================================

@register(
    name="macd_5min",
    label="MACD·5分钟K线 (12/26/9·日内)",
    group="技术指标",
    description="基于 5 分钟收盘价的经典参数 12/26/9 (快线慢线指 K 线根数, 非日历日)。偏日内节奏。A 股现货 T+1: 当日买入次日才能卖, 若更关心中线波段可改用「MACD·日K线」。",
    scenario="看盘内几分钟到数小时的涨跌节奏，希望信号跟得上分时波动、做短线参考时。",
    rules="用 5 分钟 K 线收盘价计算 MACD(12/26/9)：DIF 从下向上穿过 DEA 为金叉（偏买入）；从上向下穿过为死叉（偏卖出）。K 线不足则保持观望。",
    example="急跌后 DIF 再次上穿 DEA，可能对应一小段反弹；震荡市里交叉会较频繁，需结合风控。",
    params=[
        _p_int("fast", "快线 EMA", 12, min=2, max=60, desc="快线周期 (根)"),
        _p_int("slow", "慢线 EMA", 26, min=5, max=120, desc="慢线周期 (根)"),
        _p_int("signal", "信号线 DEA", 9, min=2, max=60, desc="DIF 的 EMA 周期"),
    ],
)
def strat_macd_5min(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "5m", 120)
    if df is None:
        return _hold("macd_5min", "无 5 分钟 K 线")
    close = df["close"].astype(float)
    return _macd_cross_from_close(close, "macd_5min", "5min")


# ============================================================
# 策略 1b: MACD 日线 (经典 12/26/9, 日 K 收盘)
# ============================================================

@register(
    name="macd_1d",
    label="MACD·日K线 (12/26/9·波段)",
    group="技术指标",
    description="基于日 K 收盘价的 12/26/9, 与常见软件「日线 MACD」一致。适合多日持仓与 T+1 下的波段决策; 信号比 5 分钟 MACD 稀疏。",
    scenario="更做隔日、波段，不想被 5 分钟频繁交叉打扰；与 A 股 T+1「今日买明日卖」的节奏更接近时。",
    rules="用日 K 收盘价算经典 MACD(12/26/9)，金叉 / 死叉含义与 5 分钟版相同，只是每根 K 代表一个交易日。",
    example="连续回调后日线出现金叉，常作为波段关注信号之一（是否下单仍看资金与风控）。",
    params=[
        _p_int("fast", "快线 EMA", 12, min=2, max=60, desc="快线周期 (根)"),
        _p_int("slow", "慢线 EMA", 26, min=5, max=120, desc="慢线周期 (根)"),
        _p_int("signal", "信号线 DEA", 9, min=2, max=60, desc="DIF 的 EMA 周期"),
    ],
)
def strat_macd_1d(code: str, market, capital: float) -> dict:
    df = _safe_kline(market, code, "1d", 250)
    if df is None:
        return _hold("macd_1d", "无日 K 线")
    close = df["close"].astype(float)
    return _macd_cross_from_close(close, "macd_1d", "日K")


# ============================================================
# 策略 2: 双均线 5min
# ============================================================

@register(
    name="dual_ma_5min",
    label="双均线 5min (5/20 EMA)",
    group="技术指标",
    description="5 分钟 K 线: 5EMA 上穿 20EMA 买入, 下穿卖出",
    scenario="喜欢「快慢线交叉」这种直观规则，且希望比日线更快反应时。",
    rules="在 5 分钟收盘价上计算 5 周期与 20 周期指数均线；快线上穿慢线 → 偏买；快线下穿慢线 → 偏卖。",
    example="横盘后快线上穿慢线，可理解为短期均线重新站到长期均线上方，常当作转强信号之一。",
    params=[
        _p_int("fast", "快线 EMA", 5, min=2, max=60, desc="快线周期 (根)"),
        _p_int("slow", "慢线 EMA", 20, min=3, max=120, desc="慢线周期 (根)"),
    ],
)
def strat_dual_ma_5min(code: str, market, capital: float) -> dict:
    fast_p = int(P("dual_ma_5min", "fast"))
    slow_p = int(P("dual_ma_5min", "slow"))
    df = _safe_kline(market, code, "5m", max(50, slow_p * 3))
    if df is None or len(df) < slow_p + 5:
        return _hold("dual_ma_5min", f"K 线不足 {slow_p + 5} 根")
    close = df["close"].astype(float)
    fast = close.ewm(span=fast_p, adjust=False).mean()
    slow = close.ewm(span=slow_p, adjust=False).mean()
    if len(fast) < 2:
        return _hold("dual_ma_5min")
    prev_diff = fast.iloc[-2] - slow.iloc[-2]
    curr_diff = fast.iloc[-1] - slow.iloc[-1]
    if prev_diff <= 0 and curr_diff > 0:
        return _signal("buy", "dual_ma_5min",
                       f"{fast_p}EMA 上穿 {slow_p}EMA, diff={curr_diff:+.3f}")
    if prev_diff >= 0 and curr_diff < 0:
        return _signal("sell", "dual_ma_5min",
                       f"{fast_p}EMA 下穿 {slow_p}EMA, diff={curr_diff:+.3f}")
    return _hold("dual_ma_5min")


# ============================================================
# 策略 2b: MA20 持股法 (日 K, 价格与 MA20 交叉)
# ============================================================
# 规则与 dual_ma_5min 同属「均线交叉」族, 换成收盘价 vs 简单 MA20:
#   - 前一日收盘在 MA20 及以下、当日收盘站上 MA20 -> buy
#   - 前一日收盘在 MA20 及以上、当日收盘跌穿 MA20 -> sell

@register(
    name="ma20_hold",
    label="MA20 持股法 (日K 突破/跌破)",
    group="技术指标",
    description="日 K 线: 收盘向上突破 MA20 买入, 向下跌破 MA20 卖出 (站上持股、跌破离场)",
    scenario="想用最简单的「20 日线」做波段过滤: 站上认为趋势转强可介入, 跌破则离场观望时。",
    rules="用日 K 收盘价计算 MA20。前一日收盘 ≤ MA20 且当日收盘 > MA20 → 偏买；前一日收盘 ≥ MA20 且当日收盘 < MA20 → 偏卖；其余观望。",
    example="整理后首日阳线收盘站上 MA20 触发买入；之后若回调收在 MA20 下方, 触发卖出离场。",
    params=[
        _p_int("window", "均线周期", 20, min=5, max=120, desc="MA 周期 (日)"),
    ],
)
def strat_ma20_hold(code: str, market, capital: float) -> dict:
    win = int(P("ma20_hold", "window"))
    df = _safe_kline(market, code, "1d", max(80, win + 10))
    if df is None or len(df) < win + 2:
        return _hold("ma20_hold", f"日 K 不足 {win + 2} 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(win).mean()
    if math.isnan(ma20.iloc[-1]) or math.isnan(ma20.iloc[-2]) or float(ma20.iloc[-1]) <= 0:
        return _hold("ma20_hold", f"MA{win} 不可用")
    prev_c = float(close.iloc[-2])
    curr_c = float(close.iloc[-1])
    prev_m = float(ma20.iloc[-2])
    curr_m = float(ma20.iloc[-1])
    if prev_c <= prev_m and curr_c > curr_m:
        return _signal(
            "buy",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 突破 MA{win} {prev_m:.2f}->{curr_m:.2f}",
        )
    if prev_c >= prev_m and curr_c < curr_m:
        return _signal(
            "sell",
            "ma20_hold",
            f"日K 收盘 {prev_c:.2f}->{curr_c:.2f} 跌破 MA{win} {prev_m:.2f}->{curr_m:.2f}",
        )
    return _hold(
        "ma20_hold",
        f"收盘 {curr_c:.2f} MA{win} {curr_m:.2f} 无穿越",
    )


# ============================================================
# 策略 3: 多因子轻量版 (MOM_1M + RSI_14 + BIAS_20 合成)
# ============================================================
# 全市场截面多因子原版是"全市场截面 + IC 加权", 单只股做不了截面比较
# 这里改成"绝对阈值"版: 三个因子分别打分 [-1, +1] 后求平均

def _multi_factor_selector(meta_df, snapshot, capital):
    """多因子全市场选股器: 用快照可得的短动量、量比、成交额做截面打分(降序取 top)."""
    import pandas as pd
    df = meta_df.copy()
    out = pd.Series(0.0, index=df.index, dtype=float)
    if df.empty:
        return out
    col = {"pct_change": "pct_change", "vol_ratio": "vol_ratio", "amount": "amount"}
    present = [k for k, v in col.items() if v in df.columns]
    for k in present:
        c = col[k]
        s = pd.to_numeric(df[c], errors="coerce")
        denom = s.max() - s.min()
        if denom and s.notna().any():
            out = out + (s - s.min()) / denom
    return out


@register(
    name="multi_factor_top",
    label="多因子轻量 (动量+RSI+乖离)",
    group="量化选股",
    description="日线 MOM_1M + RSI_14 + BIAS_20 合成 alpha, > 0.3 买, < -0.3 卖",
    scenario="单只股票也想用「动量 + 超买超卖 + 乖离」综合打分，而不是只看一条均线时。",
    rules="在日 K 上算三类因子并归一后取平均得到 alpha；alpha 高于阈值偏买入，低于负阈值偏卖出；中间区间观望。",
    example="alpha 从负区间一举升到阈值之上，表示多因子同时转强，可能触发买入侧信号。",
    selector=_multi_factor_selector,
    params=[
        _p_float("sell_th", "卖出阈值", -0.30, min=-1.0, max=0.0, step=0.05, desc="alpha 低于此值偏卖"),
        _p_int("mom_days", "动量周期", 21, min=5, max=120, desc="N 日涨跌幅"),
        _p_float("mom_cap", "动量饱和幅度", 0.15, min=0.01, max=1.0, step=0.01, desc="涨幅达到该幅度记满分"),
        _p_int("rsi_period", "RSI 周期", 14, min=2, max=60, desc="RSI 计算周期"),
        _p_int("bias_window", "乖离均线", 20, min=5, max=120, desc="BIAS 用的 MA 周期"),
    ],
)
def strat_multi_factor(code: str, market, capital: float) -> dict:
    buy_th = float(P("multi_factor_top", "buy_th"))
    sell_th = float(P("multi_factor_top", "sell_th"))
    mom_days = int(P("multi_factor_top", "mom_days"))
    mom_cap = float(P("multi_factor_top", "mom_cap"))
    rsi_period = int(P("multi_factor_top", "rsi_period"))
    bias_win = int(P("multi_factor_top", "bias_window"))

    need = max(mom_days + 1, rsi_period + 1, bias_win, 30)
    df = _safe_kline(market, code, "1d", max(200, need + 20))
    if df is None or len(df) < need:
        return _hold("multi_factor_top", f"日 K 线不足 {need} 根")
    close = df["close"].astype(float)

    # 因子 1: N 日动量, 映射到 [-1, +1] (mom_cap 涨幅 -> 1.0)
    if len(close) >= mom_days + 1:
        mom_1m = close.iloc[-1] / close.iloc[-(mom_days + 1)] - 1.0
    else:
        mom_1m = 0.0
    f_mom = max(-1.0, min(1.0, mom_1m / mom_cap))

    # 因子 2: RSI, 偏离 50 越远动能越强; > 70 超买 (反转减分), < 30 超卖 (反转加分)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    if rsi > 70:
        f_rsi = -((rsi - 70) / 30)        # 超买扣分
    elif rsi < 30:
        f_rsi = (30 - rsi) / 30           # 超卖加分 (反转买入)
    else:
        f_rsi = (rsi - 50) / 50           # 中间区随动能

    # 因子 3: BIAS (乖离率), 过涨易回调 (取负号)
    if len(close) >= bias_win:
        ma20 = close.rolling(bias_win).mean().iloc[-1]
        bias = (close.iloc[-1] - ma20) / ma20 if ma20 > 0 else 0.0
    else:
        bias = 0.0
    f_bias = max(-1.0, min(1.0, -bias / 0.10))   # +/- 10% 乖离 -> +/- 1.0

    alpha = (f_mom + f_rsi + f_bias) / 3
    reason = (f"MOM_{mom_days}D={mom_1m:+.2%} RSI={rsi:.1f} BIAS={bias:+.2%} "
              f"-> alpha={alpha:+.2f}")

    if alpha > buy_th:
        return _signal("buy", "multi_factor_top", reason)
    if alpha < sell_th:
        return _signal("sell", "multi_factor_top", reason)
    return _hold("multi_factor_top", reason)


# ============================================================
# 策略 4: 龙头动量 (单只股票版)
# ============================================================
# dragon_picker 全市场版是全市场涨幅榜, 这里只看单只股自身:
#   - 当日涨幅 (从今日开盘到现在)
#   - 量比 (今日累计成交量 / 过去 5 日均量)
#   - 价位
# 满足 5 法则 (除"涨幅榜排名"无法在单股视角拿到) -> buy
# 当日跌幅 > 3% 或 大幅放量下跌 -> sell

@register(
    name="dragon_picker",
    label="龙头首板战法 (5 法则简化)",
    group="龙头动量",
    description="当日涨幅+量比+价位综合打分, 龙头分 >= 1.5 买入, 跌破日内回撤线卖出",
    scenario="关注当日强势、放量上攻的短线博弈（单票版龙头思路），愿意承担较大波动时。",
    rules="综合当日涨幅、量比、股价区间等打分；满足涨幅、量比、价位且总分够高时偏买；当日大跌或从高点明显回撤时偏卖。",
    example="当日涨幅已超过约 5%、量比显著放大、股价在约 30 元下方且综合分达标，可能触发买入侧信号。",
    params=[
        _p_float("buy_chg", "买入涨幅门槛", 0.05, min=0.0, max=0.20, step=0.01, desc="当日涨幅需超过此值 (5%)"),
        _p_float("buy_vol_ratio", "买入量比门槛", 2.0, min=0.5, max=10.0, step=0.1, desc="量比需超过此值"),
        _p_float("buy_price_max", "买入价格上限", 30.0, min=0.0, max=1000.0, step=1.0, desc="现价低于此价才考虑买入"),
        _p_float("buy_score", "买入龙头分门槛", 1.5, min=0.0, max=5.0, step=0.1, desc="综合分需达到此值"),
        _p_float("sell_chg", "卖出跌幅门槛", 0.03, min=0.0, max=0.20, step=0.01, desc="当日跌幅超过此值卖出 (3%)"),
        _p_float("sell_drawdown", "卖出回撤门槛", 0.03, min=0.0, max=0.20, step=0.01, desc="从日内高点回撤超过此值卖出 (3%)"),
    ],
)
def strat_dragon(code: str, market, capital: float) -> dict:
    buy_chg = float(P("dragon_picker", "buy_chg"))
    buy_vr = float(P("dragon_picker", "buy_vol_ratio"))
    buy_pmax = float(P("dragon_picker", "buy_price_max"))
    buy_score = float(P("dragon_picker", "buy_score"))
    sell_chg = float(P("dragon_picker", "sell_chg"))
    sell_dd = float(P("dragon_picker", "sell_drawdown"))

    # 拉日线: 用昨日收盘 / 5 日均量做基准
    df_d = _safe_kline(market, code, "1d", 10)
    if df_d is None or len(df_d) < 6:
        return _hold("dragon_picker", "日 K 不足 6 根")
    prev_close = float(df_d["close"].iloc[-2]) if len(df_d) >= 2 else float(df_d["close"].iloc[-1])
    avg_vol_5d = float(df_d["volume"].iloc[-6:-1].mean()) if "volume" in df_d.columns else 0

    # 拉今日 5min K 线累加 (用 5min 而不是 1m, 因日线 market 常与 5m 分钟流配套)
    df_min = _safe_kline(market, code, "5m", 80)
    if df_min is None or len(df_min) == 0:
        return _hold("dragon_picker", "分钟 K 不足")

    # 取最新一根作为现价
    cur_price = float(df_min["close"].iloc[-1])
    today_str = str(df_min.index[-1])[:10]
    today_bars = df_min[df_min.index.astype(str).str[:10] == today_str]
    if len(today_bars) == 0:
        return _hold("dragon_picker", "今日分钟 K 缺失")
    today_high = float(today_bars["high"].max()) if "high" in today_bars.columns else cur_price
    today_vol = float(today_bars["volume"].sum()) if "volume" in today_bars.columns else 0

    # 当日涨幅 vs 昨收
    day_change = (cur_price / prev_close - 1.0) if prev_close > 0 else 0.0
    # 量比 (今日累计 vs 5 日均)
    vol_ratio = (today_vol / avg_vol_5d) if avg_vol_5d > 0 else 0.0

    # 出场: 从当日最高点回撤超门槛, 或 当日整体跌幅超门槛 -> sell
    drawdown = (cur_price / today_high - 1.0) if today_high > 0 else 0.0
    if day_change < -sell_chg or drawdown < -sell_dd:
        return _signal("sell", "dragon_picker",
                       f"日内 chg={day_change:+.2%} 回撤={drawdown:+.2%}")

    # 入场打分 (复用 calc_dragon_score 思路, 简化无市值)
    score = 0.0
    if day_change > 0.09:
        score += 0.5      # 接近涨停减分
    else:
        score += min(max(day_change, 0) * 10, 1.0)
    score += min(vol_ratio / 3, 1.5)
    if cur_price < 20:
        score += 0.5
    elif cur_price <= 30:
        score += 0.2

    reason = (f"日涨={day_change:+.2%} 量比={vol_ratio:.2f} "
              f"价={cur_price:.2f} 龙头分={score:.2f}")

    # 阈值: 涨幅 + 量比 + 价位 + 综合分 (均可配置)
    if (day_change > buy_chg and vol_ratio > buy_vr and cur_price < buy_pmax
            and score >= buy_score):
        return _signal("buy", "dragon_picker", reason)
    return _hold("dragon_picker", reason)


# ============================================================
# 策略 6: RSI 反转（经典超买超卖 + 穿越确认）
# ============================================================
# 经典 RSI: RSI<30 买、RSI>70 卖, period=14
# 这里在原逻辑基础上 + "穿越确认"（减少在强趋势里反复触发）:
#   - RSI 上穿 30  -> 买  (从超卖反弹, 比单纯 <30 更稳, 避免抄底抄到一半)
#   - RSI 下穿 70  -> 卖  (从超买回落, 减少在强趋势里被洗下车)
# 适用: 震荡 / 反转性强的票 (银行 / 公用 / 部分大盘蓝筹)

@register(
    name="rsi_reversal",
    label="RSI 反转 (14·30/70 穿越)",
    group="技术指标",
    description="日 K RSI(14) 上穿 30 买入, 下穿 70 卖出 (经典 RSI + 穿越确认)",
    scenario="震荡市 / 反转性强的票（银行、公用事业、部分大盘蓝筹），不追趋势, 抓「跌透了反弹」「涨过头回落」的边界。",
    rules="日 K 收盘算 RSI(14)。RSI 从 ≤30 上穿 30 -> 买（超卖反弹确认）；从 ≥70 下穿 70 -> 卖（超买回落确认）；其他时间观望。比裸 <30 / >70 信号更稳, 避免在强趋势里被反复打脸。",
    example="平安银行 RSI 跌到 28, 次日反弹收 32 -> 触发买入；后续涨到 RSI 73, 次日回落收 68 -> 触发卖出。",
    params=[
        _p_int("period", "RSI 周期", 14, min=2, max=60, desc="RSI 计算周期 (日)"),
        _p_int("oversold", "超卖线", 30, min=5, max=50, desc="RSI 上穿此值买入"),
        _p_int("overbought", "超买线", 70, min=50, max=95, desc="RSI 下穿此值卖出"),
    ],
)
def strat_rsi_reversal(code: str, market, capital: float) -> dict:
    period = int(P("rsi_reversal", "period"))
    os_line = int(P("rsi_reversal", "oversold"))
    ob_line = int(P("rsi_reversal", "overbought"))
    need = period + 6
    df = _safe_kline(market, code, "1d", max(60, need + 20))
    if df is None or len(df) < need:
        return _hold("rsi_reversal", f"日 K 不足 {need} 根")
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)
    if len(rsi) < 2 or math.isnan(rsi.iloc[-1]) or math.isnan(rsi.iloc[-2]):
        return _hold("rsi_reversal", "RSI 序列不足")
    prev = float(rsi.iloc[-2])
    curr = float(rsi.iloc[-1])
    if prev <= os_line < curr:
        return _signal("buy", "rsi_reversal",
                       f"RSI 上穿 {os_line}: {prev:.1f} -> {curr:.1f} (超卖反弹)")
    if prev >= ob_line > curr:
        return _signal("sell", "rsi_reversal",
                       f"RSI 下穿 {ob_line}: {prev:.1f} -> {curr:.1f} (超买回落)")
    return _hold("rsi_reversal", f"RSI={curr:.1f} 中性区")


# ============================================================
# 策略 7: 布林带均值回归 (BollingerBands)
# ============================================================
# 经典版: 触下轨买、触上轨卖, period=20, dev=2.0
# 完全保留原参数; 加 "中轨止盈" 防止持仓涨到中轨就吐回去
#   - 收盘 < 下轨   -> 买
#   - 收盘 > 上轨   -> 卖 (止盈)
#   - 收盘 < 中轨且前一日 >= 中轨 -> 卖 (跌破中轨止盈)

@register(
    name="boll_revert",
    label="布林带均值回归 (20·2σ)",
    group="技术指标",
    description="日 K 布林带(20, 2σ) 触下轨买、触上轨卖 + 跌破中轨止盈",
    scenario="波动有规律的票, 价格围绕中线均值上下震荡, 想做「触下轨吃货, 触上轨止盈」的均值回归操作。",
    rules="日 K 收盘价计算 MA20 与 ±2σ 三条线。收盘 < 下轨 -> 买; 收盘 > 上轨 -> 卖; 持仓时收盘从中轨上方跌破中轨 -> 卖 (止盈, 防回吐)。波动率扩张到强趋势时容易追涨杀跌, 需配合风控。",
    example="纳指 ETF 价格触下轨 1.65 触发买入; 反弹到 1.78 突破上轨 -> 卖出止盈。",
    params=[
        _p_int("period", "均线周期", 20, min=5, max=120, desc="布林带中轨 MA 周期"),
        _p_float("num_std", "标准差倍数", 2.0, min=0.5, max=5.0, step=0.1, desc="上下轨 = 中轨 ± N×σ"),
    ],
)
def strat_boll_revert(code: str, market, capital: float) -> dict:
    period = int(P("boll_revert", "period"))
    nstd = float(P("boll_revert", "num_std"))
    need = period + 5
    df = _safe_kline(market, code, "1d", max(60, need + 20))
    if df is None or len(df) < need:
        return _hold("boll_revert", f"日 K 不足 {need} 根")
    close = df["close"].astype(float)
    ma = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = ma + nstd * std
    lower = ma - nstd * std
    if math.isnan(ma.iloc[-1]):
        return _hold("boll_revert", "BOLL 序列不足")
    cur = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    mid_cur = float(ma.iloc[-1])
    mid_prev = float(ma.iloc[-2]) if not math.isnan(ma.iloc[-2]) else mid_cur
    up_cur = float(upper.iloc[-1])
    lo_cur = float(lower.iloc[-1])
    if cur < lo_cur:
        return _signal("buy", "boll_revert",
                       f"收盘 {cur:.2f} < 下轨 {lo_cur:.2f} (中轨 {mid_cur:.2f})")
    if cur > up_cur:
        return _signal("sell", "boll_revert",
                       f"收盘 {cur:.2f} > 上轨 {up_cur:.2f} (中轨 {mid_cur:.2f})")
    if prev >= mid_prev and cur < mid_cur:
        return _signal("sell", "boll_revert",
                       f"跌破中轨 {mid_cur:.2f} 止盈 (前 {prev:.2f}/中 {mid_prev:.2f})")
    return _hold("boll_revert",
                 f"在 [{lo_cur:.2f}, {up_cur:.2f}] 之间 中轨 {mid_cur:.2f}")


# ============================================================
# 策略 8: 乖离率均值回归 (BIAS)
# ============================================================
# 经典阈值: BIAS<-6% 买, BIAS>3% 卖, MA20
# 适用: 短期超跌反弹 / 涨多回调 -- 节奏型票 (大盘蓝筹反弹)

@register(
    name="bias_revert",
    label="乖离率均值回归 (BIAS·20)",
    group="技术指标",
    description="日 K 乖离率 < -6% 买入 (超跌), > 3% 卖出 (超涨)",
    scenario="跟随 20 日均线节奏运行的票, 想做「跌得离均线太远 -> 反弹补涨」「涨得离均线太远 -> 回调收口」的中期均值回归。",
    rules="BIAS = (收盘 - MA20) / MA20。BIAS < -6% -> 偏买 (超跌反弹); BIAS > 3% -> 偏卖 (涨过头, 注意不对称: 上涨节奏比下跌温和)。趋势单边市 (持续创新高 / 新低) 容易钝化。",
    example="贵州茅台日 K 收盘 1450, MA20 在 1545, BIAS = -6.15% -> 触发买入; 涨到 BIAS = 3.5% -> 卖出。",
    params=[
        _p_int("window", "均线周期", 20, min=5, max=120, desc="BIAS 用的 MA 周期"),
        _p_float("buy_th", "买入阈值", -0.06, min=-0.5, max=0.0, step=0.01, desc="BIAS 低于此值偏买 (-6%)"),
        _p_float("sell_th", "卖出阈值", 0.03, min=0.0, max=0.5, step=0.01, desc="BIAS 高于此值偏卖 (3%)"),
    ],
)
def strat_bias_revert(code: str, market, capital: float) -> dict:
    win = int(P("bias_revert", "window"))
    buy_th = float(P("bias_revert", "buy_th"))
    sell_th = float(P("bias_revert", "sell_th"))
    need = win + 5
    df = _safe_kline(market, code, "1d", max(60, need + 20))
    if df is None or len(df) < need:
        return _hold("bias_revert", f"日 K 不足 {need} 根")
    close = df["close"].astype(float)
    ma20 = close.rolling(win).mean()
    if math.isnan(ma20.iloc[-1]) or ma20.iloc[-1] <= 0:
        return _hold("bias_revert", f"MA{win} 不可用")
    cur = float(close.iloc[-1])
    bias = (cur - float(ma20.iloc[-1])) / float(ma20.iloc[-1])
    reason = f"BIAS={bias:+.2%} 收盘 {cur:.2f}/MA{win} {float(ma20.iloc[-1]):.2f}"
    if bias < buy_th:
        return _signal("buy", "bias_revert", "超跌 " + reason)
    if bias > sell_th:
        return _signal("sell", "bias_revert", "超涨 " + reason)
    return _hold("bias_revert", reason)


# ============================================================
# 策略 9: 海龟唐奇安通道（经典简化版）
# ============================================================
# 原版含: 唐奇安通道 20 入 / 10 出 + ATR 仓位 + 金字塔加仓 + 2N 止损
# 本工作台为「信号 + 路由」框架, 仓位由风控统一管, 只输出方向信号:
#   - 收盘 > 过去 20 日最高 (不含今日) -> buy  (突破入场)
#   - 收盘 < 过去 10 日最低 (不含今日) -> sell (跌破出场)
# 适用: 趋势性强的票 (科技成长 / 宽基 ETF / 跨境 ETF)

@register(
    name="turtle_donchian",
    label="海龟唐奇安通道 (20入/10出)",
    group="趋势跟随",
    description="日 K 突破 20 日新高买入, 跌破 10 日新低卖出 (海龟唐奇安简化版, 不含 ATR 加仓)",
    scenario="趋势性强的标的: 科技成长 (中芯/宁德), 宽基 ETF, 跨境 ETF (纳指 ETF), 想等趋势确认再上车 / 跌破就走人时。",
    rules="收盘 > 过去 20 个交易日最高价 (不含今日) -> 买; 收盘 < 过去 10 个交易日最低价 (不含今日) -> 卖。趋势市赚大段, 震荡市频繁假突破被打脸 (需要止损 + 选标的)。",
    example="中芯国际经过整理, 收盘突破前 20 日最高的 102.5 -> 买入; 后续高位震荡, 跌破前 10 日最低 95.2 -> 卖出离场。",
    params=[
        _p_int("entry_days", "入场通道", 20, min=5, max=120, desc="突破 N 日新高买入"),
        _p_int("exit_days", "出场通道", 10, min=3, max=60, desc="跌破 M 日新低卖出"),
    ],
)
def strat_turtle_donchian(code: str, market, capital: float) -> dict:
    entry_days = int(P("turtle_donchian", "entry_days"))
    exit_days = int(P("turtle_donchian", "exit_days"))
    need = max(entry_days, exit_days) + 2
    df = _safe_kline(market, code, "1d", max(60, need + 20))
    if df is None or len(df) < need:
        return _hold("turtle_donchian", f"日 K 不足 {need} 根")
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    cur = float(close.iloc[-1])
    entry_high = float(high.iloc[-(entry_days + 1):-1].max())   # 不含今日的过去 N 日
    exit_low = float(low.iloc[-(exit_days + 1):-1].min())       # 不含今日的过去 M 日
    if cur > entry_high:
        return _signal("buy", "turtle_donchian",
                       f"收盘 {cur:.2f} 突破 {entry_days} 日高 {entry_high:.2f}")
    if cur < exit_low:
        return _signal("sell", "turtle_donchian",
                       f"收盘 {cur:.2f} 跌破 {exit_days} 日低 {exit_low:.2f}")
    return _hold("turtle_donchian",
                 f"在 [{exit_low:.2f}, {entry_high:.2f}] 之间, 收盘 {cur:.2f}")


# ============================================================
# 策略 5: 经典网格 (无状态版)
# ============================================================
# 经典网格为有状态「格子位置变化触发」策略
# 这里改成"无状态阈值版": 每轮根据当前价在网格中的位置判断
#   - 价格落到下半区 (<= 第 2 格) -> buy (低吸)
#   - 价格冲到上半区 (>= 倒数第 2 格) -> sell (高抛)
# 配合 5min K 线的短期动能避免趋势单边市追高/抄底

@register(
    name="grid_classic",
    label="经典网格 (60 日区间 8 格)",
    group="震荡网格",
    description="过去 60 日 high/low 切 8 格, 价格在底部 2 格买, 顶部 2 格卖 (适合震荡市)",
    scenario="判断该股在一段时期内主要在箱体内震荡，想做「低位多吸、高位分批减」时。",
    rules="取约 60 个交易日最高价与最低价划成 8 格；现价落在最下两格偏买、最上两格偏卖；冲出区间上下沿另有止损/止盈类处理。",
    example="长期在箱体内运行时，价格回到区间下沿附近可能出现低吸类信号；单边趋势市则容易反复打脸。",
    params=[
        _p_int("window", "区间回看", 60, min=20, max=250, desc="用过去 N 日 high/low 划区间"),
        _p_int("grids", "网格数", 8, min=3, max=30, desc="区间等分成 N 格"),
        _p_int("buy_levels", "低吸格数", 2, min=1, max=10, desc="落在底部 N 格偏买"),
        _p_int("sell_levels", "高抛格数", 2, min=1, max=10, desc="落在顶部 N 格偏卖"),
        _p_float("margin", "边界缓冲", 0.02, min=0.0, max=0.2, step=0.01, desc="区间上下沿外扩比例 (2%)"),
    ],
)
def strat_grid_classic(code: str, market, capital: float) -> dict:
    win = int(P("grid_classic", "window"))
    grids = int(P("grid_classic", "grids"))
    buy_lv = int(P("grid_classic", "buy_levels"))
    sell_lv = int(P("grid_classic", "sell_levels"))
    margin_pct = float(P("grid_classic", "margin"))

    df_d = _safe_kline(market, code, "1d", max(80, win + 20))
    if df_d is None or len(df_d) < win:
        return _hold("grid_classic", f"日 K 不足 {win} 根")
    high_60 = float(df_d["high"].iloc[-win:].max())
    low_60 = float(df_d["low"].iloc[-win:].min())
    if high_60 <= low_60:
        return _hold("grid_classic", "网格区间无效")

    # 上下预留缓冲
    margin = (high_60 - low_60) * margin_pct
    upper = high_60 + margin
    lower = low_60 - margin
    grid_size = (upper - lower) / grids

    df_min = _safe_kline(market, code, "5m", 5)
    if df_min is None or len(df_min) == 0:
        cur_price = float(df_d["close"].iloc[-1])
    else:
        cur_price = float(df_min["close"].iloc[-1])

    grid_idx = int((cur_price - lower) / grid_size) if grid_size > 0 else grids // 2
    grid_idx = max(0, min(grids - 1, grid_idx))

    reason = (f"区间[{lower:.2f},{upper:.2f}] 当前={cur_price:.2f} "
              f"位于第 {grid_idx + 1}/{grids} 格")

    # 出界处理: 跌破下界 (满仓套牢) 或 涨破上界 (踏空)
    if cur_price < lower:
        return _signal("sell", "grid_classic", f"跌破下界 {lower:.2f} -> 止损 " + reason)
    if cur_price > upper:
        return _signal("sell", "grid_classic", f"涨破上界 {upper:.2f} -> 止盈 " + reason)

    if grid_idx <= buy_lv - 1:        # 底部 N 格
        return _signal("buy", "grid_classic", "底部低吸 " + reason)
    if grid_idx >= grids - sell_lv:   # 顶部 N 格
        return _signal("sell", "grid_classic", "顶部高抛 " + reason)
    return _hold("grid_classic", reason)


# ============================================================
# 策略 10: ML 概率因子 (XGBoost 滚动训练)
# ============================================================
# 50+ 技术因子 + XGBoost 滚动训练 -> 输出每日涨概率
#   - prob > 0.60 -> buy  (模型认为明日涨概率显著高于均匀)
#   - prob < 0.40 -> sell (模型认为明日跌概率显著高)
#   - 中间区间 hold
# 与 walk_forward / 回测引擎共用 ml_strategy.ml_prob_runner.run_ml_prob
# (保证 WF 上的过拟合诊断与实盘信号口径一致)

@register(
    name="ml_prob",
    label="ML 概率因子 (XGBoost)",
    group="机器学习",
    description="50+ 技术因子 + XGBoost 滚动训练, 涨概率 > 0.60 买 / < 0.40 卖. 滚动重训本身就是 walk-forward, OOS 衰减比单一指标策略小.",
    scenario="想用机器学习模型做信号, 不依赖单一指标, 让模型从 50+ 因子里自己学有效组合; 标的有 ≥ 250 根日 K 历史数据 (训练样本足够).",
    rules=("拉至少 250 根日 K -> 计算 50+ 技术因子 -> 用过去 120 天滚动训练 XGBoost (每 20 天重训一次, 实现内置 walk-forward) -> 输出每日涨概率. "
           "prob > 0.60 偏买; prob < 0.40 偏卖; 0.40~0.60 区间观望. "
           "首轮调用需训练 ~5 秒, 同一交易日内重复调用走缓存."),
    example="贵州茅台 600 根日 K, 模型输出当日 prob=0.68 (高于均匀 0.5) -> 偏买; 次日 prob=0.32 -> 偏卖.",
    params=[
        _p_float("buy_th", "买入概率阈值", 0.60, min=0.5, max=0.95, step=0.01, desc="涨概率高于此值偏买"),
        _p_float("sell_th", "卖出概率阈值", 0.40, min=0.05, max=0.5, step=0.01, desc="涨概率低于此值偏卖"),
        _p_int("train_days", "训练窗口", 120, min=30, max=500, desc="滚动训练样本天数"),
        _p_int("retrain_interval", "重训间隔", 20, min=1, max=120, desc="每 N 天重训一次"),
    ],
)
def strat_ml_prob(code: str, market, capital: float) -> dict:
    buy_th = float(P("ml_prob", "buy_th"))
    sell_th = float(P("ml_prob", "sell_th"))
    train_days = int(P("ml_prob", "train_days"))
    retrain_interval = int(P("ml_prob", "retrain_interval"))

    df = _safe_kline(market, code, "1d", 250)
    if df is None or len(df) < 200:
        return _hold("ml_prob", f"日 K 不足 200 根 (ML 需要训练样本, 当前 {0 if df is None else len(df)})")

    try:
        from ml_strategy.ml_prob_runner import run_ml_prob
        out = run_ml_prob(
            df,
            train_days=train_days,
            retrain_interval=retrain_interval,
            buy_th=buy_th,
            sell_th=sell_th,
            horizon=1,
            model_type="xgboost",
            code=code,
            verbose=False,
        )
    except Exception as e:
        return _hold("ml_prob", f"ML 计算异常: {type(e).__name__}: {e}")

    p = float(out["meta"]["last_prob"])
    n_pred = int(out["meta"]["n_train_predict"])
    cached = " (cache)" if out.get("cached") else ""

    if p > buy_th:
        return _signal("buy", "ml_prob",
                       f"ML 涨概率 {p:.2%} > {buy_th:.0%}{cached} (滚动样本 {n_pred} 天)")
    if p < sell_th:
        return _signal("sell", "ml_prob",
                       f"ML 涨概率 {p:.2%} < {sell_th:.0%}{cached} (滚动样本 {n_pred} 天)")
    return _hold("ml_prob",
                 f"ML 涨概率 {p:.2%} 中性区{cached} (滚动样本 {n_pred} 天)")


# ============================================================
# 策略 11: 一阳穿三线 (MA5/10/20)
# ============================================================
# 经典 K 线形态: 一根阳线同时上穿 MA5/MA10/MA20 三条均线 -> 看涨
#   - 前一日收盘在三条均线下方, 当日收阳, 收盘站上三条均线 -> buy
#   - 反过来, 一根阴线同时跌破三条均线 -> sell
# 适用: 横盘整理后放量突破, 趋势启动初期

@register(
    name="one_yang_three_lines",
    label="一阳穿三线 (MA5/10/20)",
    group="技术指标",
    description="日 K 一根阳线同时上穿 MA5/10/20 三条均线买入, 一根阴线同时跌破三线卖出",
    scenario="横盘整理后期，期待一根放量中长阳确认趋势启动、站上短期 + 中期均线时。",
    rules="日 K 计算 MA5/MA10/MA20。前一日收盘 ≤ 三线最低值，且当日收阳、当日收盘 > 三线最高值 -> 买（一阳穿三线）；反之，前一日收盘 ≥ 三线最高值、当日收阴、收盘 < 三线最低值 -> 卖（一阴穿三线）。其余观望。",
    example="整理后某日开盘在 MA20 下方，收盘站上 MA5/MA10/MA20 且收中阳 -> 触发买入；之后高位收长阴同时跌破三线 -> 触发卖出。",
    params=[
        _p_int("ma_short", "短均线", 5, min=2, max=60, desc="第一根均线周期"),
        _p_int("ma_mid", "中均线", 10, min=3, max=120, desc="第二根均线周期"),
        _p_int("ma_long", "长均线", 20, min=5, max=250, desc="第三根均线周期"),
    ],
)
def strat_one_yang_three_lines(code: str, market, capital: float) -> dict:
    p_short = int(P("one_yang_three_lines", "ma_short"))
    p_mid = int(P("one_yang_three_lines", "ma_mid"))
    p_long = int(P("one_yang_three_lines", "ma_long"))
    need = p_long + 5
    df = _safe_kline(market, code, "1d", max(80, need + 20))
    if df is None or len(df) < need:
        return _hold("one_yang_three_lines", f"日 K 不足 {need} 根")
    close = df["close"].astype(float)

    ma_s = close.rolling(p_short).mean()
    ma_m = close.rolling(p_mid).mean()
    ma_l = close.rolling(p_long).mean()

    if math.isnan(ma_l.iloc[-2]) or math.isnan(ma_l.iloc[-1]):
        return _hold("one_yang_three_lines", "均线序列不足")

    prev_c = float(close.iloc[-2])
    cur_c = float(close.iloc[-1])

    # 当日开盘价 (判断阴阳线); 缺 open 列时退化为 昨收 作近似
    if "open" in df.columns and not math.isnan(float(df["open"].iloc[-1])):
        cur_o = float(df["open"].iloc[-1])
    else:
        cur_o = prev_c

    s_prev = float(ma_s.iloc[-2]);  s_cur = float(ma_s.iloc[-1])
    m_prev = float(ma_m.iloc[-2]);  m_cur = float(ma_m.iloc[-1])
    l_prev = float(ma_l.iloc[-2]);  l_cur = float(ma_l.iloc[-1])

    prev_min = min(s_prev, m_prev, l_prev)
    prev_max = max(s_prev, m_prev, l_prev)
    cur_min = min(s_cur, m_cur, l_cur)
    cur_max = max(s_cur, m_cur, l_cur)
    ma_tag = f"MA{p_short}/{p_mid}/{p_long}"

    # 一阳穿三线: 昨日收盘在三线最低值之下, 今日收阳且站上三线最高值
    if prev_c <= prev_min and cur_c > cur_max and cur_c > cur_o:
        return _signal(
            "buy", "one_yang_three_lines",
            f"一阳穿三线: 收盘 {prev_c:.2f}->{cur_c:.2f} 站上 {ma_tag} "
            f"[{s_cur:.2f}/{m_cur:.2f}/{l_cur:.2f}]",
        )
    # 一阴穿三线: 昨日收盘在三线最高值之上, 今日收阴且跌破三线最低值
    if prev_c >= prev_max and cur_c < cur_min and cur_c < cur_o:
        return _signal(
            "sell", "one_yang_three_lines",
            f"一阴穿三线: 收盘 {prev_c:.2f}->{cur_c:.2f} 跌破 {ma_tag} "
            f"[{s_cur:.2f}/{m_cur:.2f}/{l_cur:.2f}]",
        )
    return _hold(
        "one_yang_three_lines",
        f"收盘 {cur_c:.2f} 均线带 [{cur_min:.2f}, {cur_max:.2f}] 未穿越三线",
    )


# ============================================================
# Router: 按 (per_stock_map, default) 路由表派发
# ============================================================

class StrategyRouter:
    """
    策略路由器 -- 给 LiveTradingLoop 用的 evaluator

    用法:
        router = StrategyRouter(
            per_stock={"600519.SH": "macd_5min", "510300.SH": "grid_classic"},
            default="macd_5min",
        )
        loop = LiveTradingLoop(..., signal_evaluator=router)
    """

    def __init__(self, per_stock: Dict[str, str], default: str = "macd_5min"):
        self.per_stock = dict(per_stock or {})
        self.default = default

    def update(self, per_stock: Optional[Dict[str, str]] = None,
               default: Optional[str] = None):
        """热更新路由表 (前端保存配置后调)"""
        if per_stock is not None:
            self.per_stock = dict(per_stock)
        if default is not None:
            self.default = default

    def __call__(self, code: str, market, capital: float) -> dict:
        name = self.per_stock.get(code, self.default)
        meta = get_strategy(name)
        if meta is None:
            return _hold("unknown", f"策略 {name} 未注册")
        try:
            return meta.evaluator(code, market, capital)
        except Exception as e:
            return _hold(name, f"策略异常: {type(e).__name__}: {e}")


# ============================================================
# 策略 12: 弱转强 (选股型, 全市场筛选 + 可配置买卖条件)
# ============================================================
# 选股在 selection_engine 的 generic_filter 完成 (多条件可独立开关):
#   - 昨日开盘价 < 阈值            (昨日弱)
#   - 今日开盘价 > 阈值            (今日转强)
#   - 昨日量能较前日增幅 %          (放量)
#   - 昨日收盘站上 5 日线
#   - 大盘(上证)当日开盘 > 阈值      (全局闸门)
# 买卖信号逻辑 (weak_to_strong.trade, 页面「参数配置」弹窗可配置, 保存即时生效):
#   - enabled:             总开关, 关闭则只选股不交易 (默认关闭, 不改变原有行为)
#   - buy.open_pct_min:    当日开盘涨幅 ≥ 该 % 才买入 (null=不限)
#   - buy.open_pct_max:    当日开盘涨幅 ≤ 该 % 才买入, 高开上限 (null=不限)
#   - buy.no_buy_up_pct:   现价较开盘价涨幅 > 该 % 放弃买入 (0=现价高于开盘价即不买)
#   - sell.take_profit_pct:浮盈 ≥ 该 % 止盈 (null=禁用)
#   - sell.stop_loss_pct:  浮亏 ≤ 该 % 止损, 负值 (null=禁用)

def _weak_trade_config() -> dict:
    """读 weak_to_strong 实例的 trade 买卖条件配置 (每次实时读, 保证页面保存即生效)"""
    try:
        from lib.strategy_runner import load_selection_config
        cfg = load_selection_config()
        inst = next((s for s in cfg.get("strategies", [])
                     if s.get("name") == "weak_to_strong"), None)
        return dict((inst or {}).get("trade") or {})
    except Exception:
        return {}


def _latest_tick_price(market, code: str) -> Optional[float]:
    """取实时 tick 最新价 (lastPrice)。

    买入判断必须以实时 tick 价作为"当前价" (分钟K只提供开盘价/最低点基准);
    tick 缺失/无效时返回 None, 调用方应放弃买入 (不基于过时分钟K价下单)。
    """
    try:
        getter = getattr(market, "get_latest_tick", None)
        if getter is None:
            return None
        px = float((getter(code) or {}).get("lastPrice") or 0)
        return px if px > 0 else None
    except Exception:
        return None


@register(
    name="weak_to_strong",
    label="弱转强",
    group="量化选股",
    description="弱转强: 昨日弱(开盘低/缩量), 今日高开放量转强, 昨日收盘站上5日线, 大盘高开时入选; 可配置买入/卖出条件",
    scenario="弱势票放量转强、或大盘高开回暖时的强势票筛选, 并在入选后按买卖条件自动跟踪买卖。",
    rules="选股: 昨日收盘涨幅低于阈值(弱)+今日开盘涨幅高于阈值(转强)+昨日量能较前日增幅达标+昨日收盘站上5日线+大盘开盘达标。买卖: 当日选股入池后 09:29-09:35 开盘买入窗口内评估买入(可设开盘涨幅门槛/开盘向上不买); 持仓浮盈达止盈线/浮亏达止损线自动卖出。各项可在策略配置弹窗独立停用。",
    example="某日大盘高开, 昨日横盘缩量低开的票今日高开放量上穿5日线 -> 入选选股池; 次日开盘满足条件触发买入, 浮盈达止盈线卖出。",
    params=[],
)
def strat_weak_to_strong(code: str, market, capital: float) -> dict:
    """弱转强: 选股在 selection_engine, 这里按 trade 配置输出买卖信号。

    买入 (无持仓且该股在选股池, 仅 09:29-09:35 窗口内):
        - 当日开盘涨幅 ≥ buy.open_pct_min (null=不限)
        - 当日开盘涨幅 ≤ buy.open_pct_max (null=不限)
        - 现价较开盘价涨幅 > buy.no_buy_up_pct 放弃买入 (0=现价高于开盘价即不买)
    卖出 (有持仓):
        - 浮盈 ≥ sell.take_profit_pct 止盈
        - 浮亏 ≤ sell.stop_loss_pct 止损 (负值)
    """
    trade = _weak_trade_config()
    if not trade.get("enabled"):
        return _hold("weak_to_strong", "弱转强: 自动买卖未启用, 仅选股")
    buy = trade.get("buy") or {}
    sell = trade.get("sell") or {}

    try:
        from lib.selection_store import query_selection_pool, query_strategy_positions
    except Exception as e:
        return _hold("weak_to_strong", f"买卖条件依赖异常: {type(e).__name__}: {e}")

    # 最新日K (买入判断需开盘/涨幅, 卖出判断需现价)
    df = None
    try:
        df = market.get_recent_kline(code, "1d", 40)
    except Exception:
        df = None

    # ---- 持仓 -> 卖出判断 ----
    try:
        pos = next((p for p in query_strategy_positions(strategy="weak_to_strong")
                    if p.get("stock_code") == code and int(p.get("volume") or 0) > 0), None)
    except Exception:
        pos = None
    if pos is not None:
        cost = float(pos.get("cost") or 0)
        cur = None
        if df is not None and len(df) >= 1:
            try:
                cur = float(df.iloc[-1]["close"])
            except Exception:
                cur = None
        if cost > 0 and cur:
            pnl_pct = (cur / cost - 1.0) * 100.0
            tp = sell.get("take_profit_pct")
            sl = sell.get("stop_loss_pct")
            if tp is not None and pnl_pct >= float(tp):
                return _signal("sell", "weak_to_strong",
                               f"止盈: 浮盈 {pnl_pct:+.2f}% ≥ {tp}% (成本 {cost:.2f}→现价 {cur:.2f})")
            if sl is not None and pnl_pct <= float(sl):
                return _signal("sell", "weak_to_strong",
                               f"止损: 浮亏 {pnl_pct:+.2f}% ≤ {sl}% (成本 {cost:.2f}→现价 {cur:.2f})")
            return _hold("weak_to_strong",
                         f"持仓浮盈 {pnl_pct:+.2f}% (止盈 {tp if tp is not None else '禁用'}% / 止损 {sl if sl is not None else '禁用'}%)")
        return _hold("weak_to_strong", "持仓成本或现价缺失, 等待")

    # ---- 无持仓 -> 买入判断 ----
    # 买入时间窗口: 仅 09:29-09:35 允许买入 (开盘窗口), 窗口外/窗口结束后放弃当天买入
    from datetime import time as _dtt, datetime as _dtn
    _t = _dtn.now().time()
    if not (_dtt(9, 29) <= _t <= _dtt(9, 35)):
        return _hold("weak_to_strong", "非买入窗口 (仅 09:29-09:35), 放弃当日买入")
    try:
        row = next((r for r in query_selection_pool(strategy="weak_to_strong")
                    if r.get("stock_code") == code), None)
    except Exception:
        row = None
    if row is None:
        return _hold("weak_to_strong", "不在选股池")
    # 取消"入选后第 N 个交易日可买": 09:26 选股入池后, 当天 09:29-09:35 窗口内即评估买入
    o_min = buy.get("open_pct_min")
    trade_date = str(row.get("trade_date") or "")[:10]

    # 买入规则: mode = open(开盘买) / dip_recover(开盘下杀后上钩)
    mode = str(buy.get("mode") or "open").strip().lower()

    # ---- 通用前置: 当日开盘涨幅 >= 阈值 (所有买入模式生效; null=不限) ----
    try:
        prev_close = float(df.iloc[-2]["close"])
        open_pct = (float(df.iloc[-1]["open"]) / prev_close - 1.0) * 100.0 \
            if prev_close > 0 else None
    except Exception:
        open_pct = None
    if o_min not in (None, ""):
        try:
            o_min_f = float(o_min)
        except Exception:
            o_min_f = None
        if o_min_f is not None and (open_pct is None or open_pct < o_min_f):
            return _hold("weak_to_strong",
                         f"今日开盘涨幅 {('%.2f%%' % open_pct) if open_pct is not None else 'N/A'} < {o_min_f}%, 不买")
    # 通用前置: 当日开盘涨幅 <= 阈值 (高开上限, 防止追高; null=不限)
    o_max = buy.get("open_pct_max")
    if o_max not in (None, ""):
        try:
            o_max_f = float(o_max)
        except Exception:
            o_max_f = None
        if o_max_f is not None and (open_pct is None or open_pct > o_max_f):
            return _hold("weak_to_strong",
                         f"今日开盘涨幅 {('%.2f%%' % open_pct) if open_pct is not None else 'N/A'} > {o_max_f}%, 高开超上限不买")

    # ---- 通用前置: 开盘向上后不买 (现价较今日开盘价涨幅超阈值则放弃; 阈值默认 0 = 现价高于开盘价即不买) ----
    try:
        _open0 = float(df.iloc[-1]["open"])
    except Exception:
        _open0 = None
    if _open0 and _open0 > 0:
        _cur = _latest_tick_price(market, code)
        if _cur is not None:
            _up_now = (_cur / _open0 - 1.0) * 100.0
            _no_up_raw = buy.get("no_buy_up_pct")
            _no_up_f = float(_no_up_raw) if _no_up_raw not in (None, "") else 0.0
            if _up_now > _no_up_f:
                return _hold("weak_to_strong",
                             f"开盘向上 {_up_now:+.2f}%, 现价高于开盘价, 放弃买入")

    # ---- 开盘买: 满足通用前置即买 (原有逻辑) ----
    if mode in ("", "open", "open_buy"):
        return _signal("buy", "weak_to_strong",
                       f"弱转强买入(开盘买): 入选 {trade_date}"
                       + (f" · 开盘涨幅 {open_pct:+.2f}%" if open_pct is not None else ""))

    # ---- 开盘下杀后回钩买 (hook / OpenHookHunter): 跟踪盘中最低点, 自最低点回升触发 ----
    # 状态机语义 (与 OpenHookHunter 一致):
    #   1) 现价 <= 开盘价*(1 - plunge_pct)       -> 确认下杀 (armed)
    #   2) 逐bar跟踪新低 low = min(low, bar.low)   (低点可能继续下移)
    #   3) 现价 >= low*(1 + rebound_pct)          -> 回升瞬间, 市价买入 (买一次即停)
    # 触发必须以实时tick价为"当前价": 分钟K回放仅预热状态(下杀/低点), tick缺失 -> 放弃买入。
    if mode in ("hook", "open_hook", "hook_hunter"):
        try:
            mdf = market.get_recent_kline(code, "1m", 300)
        except Exception:
            mdf = None
        if mdf is None or len(mdf) < 5:
            return _hold("weak_to_strong", "分时数据不足, 等待回钩信号")
        try:
            open0 = float(mdf.iloc[0]["open"])          # 今日开盘价
        except Exception:
            return _hold("weak_to_strong", "分时数据解析异常, 等待")
        if open0 <= 0:
            return _hold("weak_to_strong", "开盘价无效, 等待")
        plunge_pct = float(buy.get("plunge_pct") or 2.0)    # 下杀: 现价较开盘价跌幅 >= 该 % (UI 以 % 存储)
        rebound_pct = float(buy.get("rebound_pct") or 0.5)  # 回钩: 现价自盘中最低点回升 >= 该 % (UI 以 % 存储)
        plunge_f = plunge_pct / 100.0
        rebound_f = rebound_pct / 100.0
        armed = False
        low = None
        for i in range(len(mdf)):                           # 历史bar回放: 只预热状态 (下杀确认+跟踪新低), 不触发买入
            _low = float(mdf.iloc[i]["low"])
            _px = float(mdf.iloc[i]["close"])
            if not armed:
                if _px <= open0 * (1 - plunge_f):       # 下杀确认
                    armed = True
                    low = _low
            else:
                low = min(low, _low)                    # 跟踪新低
        # ---- 触发判断必须基于实时tick价; tick 缺失 -> 放弃买入 (不基于过时分钟K价下单) ----
        cur = _latest_tick_price(market, code)
        if cur is None:
            return _hold("weak_to_strong", "无实时tick价, 放弃买入")
        if not armed:
            if cur <= open0 * (1 - plunge_f):           # tick 触发下杀
                armed = True
                low = cur
        else:
            low = min(low, cur)                         # tick 可能创新低
        if armed and cur >= low * (1 + rebound_f):      # tick 自最低回升 -> 触发
            dip_pct_now = (low / open0 - 1.0) * 100.0
            back_pct = (cur / low - 1.0) * 100.0
            return _signal("buy", "weak_to_strong",
                           f"弱转强买入(下杀回钩·tick): 入选 {trade_date}"
                           f" · 最低跌 {dip_pct_now:+.2f}% → 自最低回升 {back_pct:+.2f}%")
        if not armed:
            return _hold("weak_to_strong",
                         f"回钩未触发: 现价 {cur:.2f} 未跌破开盘 {open0:.2f} 的 -{plunge_pct}%, 等待下杀")
        return _hold("weak_to_strong",
                     f"已下杀但回钩未确认: 自最低 {low:.2f} 需回升 {rebound_pct}%, 当前现价 {cur:.2f}")

    # ---- 开盘下杀后上钩买 (dip_recover) ----
    # 需要当日分钟K: 开盘后最低价较开盘价跌幅 >= dip_pct, 且现价较开盘价回升 >= recover_pct
    try:
        mdf = market.get_recent_kline(code, "1m", 300)
    except Exception:
        mdf = None
    if mdf is None or len(mdf) < 5:
        return _hold("weak_to_strong", "分时数据不足, 等待上钩信号")
    try:
        open0 = float(mdf.iloc[0]["open"])          # 今日开盘价
        low_min = float(mdf["low"].min())           # 开盘后最低价
    except Exception:
        return _hold("weak_to_strong", "分时数据解析异常, 等待")
    # 当前价必须用实时 tick; tick 缺失 -> 放弃买入 (不基于过时分钟K价下单)
    cur = _latest_tick_price(market, code)
    if cur is None:
        return _hold("weak_to_strong", "无实时tick价, 放弃买入")
    low_min = min(low_min, cur)                     # tick 价也可能创新低, 一并跟踪最低点
    dip_pct = float(buy.get("dip_pct") or 2.0)      # 下杀幅度阈值
    rec_pct = float(buy.get("recover_pct") or 0.5)  # 上钩回升阈值
    if open0 > 0:
        dipped = (low_min / open0 - 1.0) * 100.0
        recovered = (cur / open0 - 1.0) * 100.0
    else:
        dipped = recovered = None
    if dipped is None or dipped > -dip_pct:
        return _hold("weak_to_strong",
                     f"开盘下杀不足: 最低跌 {('%.2f%%' % dipped) if dipped is not None else 'N/A'} > -{dip_pct}%, 等待下杀")
    if recovered < rec_pct:
        return _hold("weak_to_strong",
                     f"已下杀 -{dip_pct}% 但上钩未确认: 现价回升 {('%.2f%%' % recovered) if recovered is not None else 'N/A'} < {rec_pct}%, wait")
    if o_min not in (None, ""):
        try:
            o_min_f = float(o_min)
        except Exception:
            o_min_f = None
        if o_min_f is not None and recovered < o_min_f:
            return _hold("weak_to_strong",
                         f"上钩回升 {('%.2f%%' % recovered) if recovered is not None else 'N/A'} < {o_min_f}%, 不买")
    return _signal("buy", "weak_to_strong",
                   f"弱转强买入(下杀上钩): 入选 {trade_date}"
                   + (f" · 最低跌 {dipped:+.2f}% → 现价回升 {recovered:+.2f}%" if dipped is not None and recovered is not None else ""))
