# -*- coding: utf-8 -*-
# 25-AI量化系统 策略注册中心 -- 将多类策略统一封装为 (code, market, capital) -> signal
"""
StrategyRegistry -- 策略注册中心 (单只股票视角)

设计理念:
    - live_loop 每轮对 watch 池的每只股票调一次 evaluator
    - 我们这里把"全市场扫描"型策略 (多因子/龙头) 也包装成"单只股票评估"模式
    - 每个策略都返回统一格式: {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str}

[技术指标]
        - one_yang_three_lines   一阳穿三线 (MA5/10/20 金叉/死叉)

    [震荡网格]
        - grid_classic   过去 60 日 high/low 切 8 格的经典网格

    [选股型]
        - weak_to_strong   弱转强 (昨日弱 + 今日高开放量转强)

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
    group: str                     # 分组 (技术指标 / 量化选股 / 震荡网格)
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



# ============================================================
# 策略 3: 多因子轻量版 (MOM_1M + RSI_14 + BIAS_20 合成)
# ============================================================
# 策略 6: RSI 反转（经典超买超卖 + 穿越确认）
# ============================================================
# 策略 7: 布林带均值回归 (BollingerBands)
# ============================================================
# 策略 8: 乖离率均值回归 (BIAS)
# ============================================================
# 策略 9: 海龟唐奇安通道（经典简化版）
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
    label="超跌反弹",
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
            per_stock={"600519.SH": "grid_classic", "510300.SH": "grid_classic"},
            default="grid_classic",
        )
        loop = LiveTradingLoop(..., signal_evaluator=router)
    """

    def __init__(self, per_stock: Dict[str, str], default: str = "grid_classic"):
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
#   - buy.mode:            买入模式: open=开盘买 / hook=下杀回钩买(tick) / dip_recover=下杀上钩(分钟K)
#   - buy.open_pct_min:    当日开盘涨幅 ≥ 该 % 才买入 (null=不限)
#   - buy.open_pct_max:    当日开盘涨幅 ≤ 该 % 才买入, 高开上限 (null=不限)
#   - buy.no_buy_up_pct:   现价较开盘价涨幅 > 该 % 放弃买入 (0=现价高于开盘价即不买)
#   - buy.plunge_pct:      hook 模式下杀阈值: 现价较开盘价跌达该 % 确认下杀
#   - buy.rebound_pct:     hook 模式回升阈值: 现价自最低点回升达该 % 触发买入
#   - buy.dip_pct:         dip_recover 模式下杀阈值: 开盘后最低较开盘价跌幅 ≥ 该 %
#   - buy.recover_pct:     dip_recover 模式上钩阈值: 现价较开盘价回升 ≥ 该 %
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
    rules="选股: 昨日收盘涨幅低于阈值(弱)+今日开盘涨幅高于阈值(转强)+昨日量能较前日增幅达标+昨日收盘站上N日线+大盘开盘达标。买卖: 当日选股入池后, 09:00:02-09:39:00 开盘买入窗口内每 30 秒评估买入, 默认下杀回钩模式(开盘后较开盘价下杀达阈值确认并跟踪最低点, 现价自最低点回升达阈值即市价买入; 可切开盘买/设高开上限/开盘向上不买); 持仓浮盈达止盈线/浮亏达止损线自动卖出。各项可在策略配置弹窗独立停用。",
    example="某日大盘高开, 昨日横盘缩量低开的票今日高开放量上穿5日线 -> 入选选股池; 次日开盘满足条件触发买入, 浮盈达止盈线卖出。",
    params=[],
)
def strat_weak_to_strong(code: str, market, capital: float) -> dict:
    """弱转强: 选股在 selection_engine, 这里按 trade 配置输出买卖信号。

    买入 (无持仓且该股在选股池, 仅 09:30:00-09:45:00 窗口内):
        - 当日开盘涨幅 ≥ buy.open_pct_min (null=不限)
        - 当日开盘涨幅 ≤ buy.open_pct_max (null=不限)
        - 现价较开盘价涨幅 > buy.no_buy_up_pct 放弃买入 (0=现价高于开盘价即不买)
        - 买入模式 buy.mode: open=开盘买 / hook=下杀回钩买(下杀达 plunge_pct% 确认,
          自最低点回升 rebound_pct% 触发) / dip_recover=下杀上钩买(按分钟K)
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
    # 买入时间窗口: 09:30:00-09:45:00 允许买入 (开盘窗口), 窗口外/窗口结束后放弃当天买入
    from datetime import time as _dtt, datetime as _dtn
    _t = _dtn.now().time()
    if not (_dtt(9, 30, 0) <= _t <= _dtt(9, 45, 0)):
        return _hold("weak_to_strong", "非买入窗口 (仅 09:30:00-09:45:00), 放弃当日买入")
    try:
        row = next((r for r in query_selection_pool(strategy="weak_to_strong")
                    if r.get("stock_code") == code), None)
    except Exception:
        row = None
    if row is None:
        return _hold("weak_to_strong", "不在选股池")
    # 取消"入选后第 N 个交易日可买": 09:27 选股入池后, 当天 09:30:00-09:45:00 窗口内每 30 秒评估买入
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
        plunge_pct = float(buy.get("plunge_pct")) if buy.get("plunge_pct") not in (None, "") else 2.0
        rebound_pct = float(buy.get("rebound_pct")) if buy.get("rebound_pct") not in (None, "") else 0.5  # 0=确认下杀后立即买
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
            # 触发点复检「开盘向上不买」: 用与触发一致的现价 cur (避免前置门控用旧tick产生竞态)
            _nuf = buy.get("no_buy_up_pct")
            _nuf = float(_nuf) if _nuf not in (None, "") else 0.0
            if open0 > 0 and (cur / open0 - 1.0) * 100.0 > _nuf:
                return _hold("weak_to_strong",
                             f"开盘向上 {((cur / open0 - 1.0) * 100.0):+.2f}%, 现价高于开盘价, 放弃买入")
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
    dip_pct = float(buy.get("dip_pct")) if buy.get("dip_pct") not in (None, "") else 2.0
    rec_pct = float(buy.get("recover_pct")) if buy.get("recover_pct") not in (None, "") else 0.5  # 0=下杀确认后立即买
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
    # 触发点复检「开盘向上不买」: 用与触发一致的现价 cur (避免前置门控用旧tick产生竞态)
    _nuf = buy.get("no_buy_up_pct")
    _nuf = float(_nuf) if _nuf not in (None, "") else 0.0
    if open0 > 0 and (cur / open0 - 1.0) * 100.0 > _nuf:
        return _hold("weak_to_strong",
                     f"开盘向上 {((cur / open0 - 1.0) * 100.0):+.2f}%, 现价高于开盘价, 放弃买入")
    return _signal("buy", "weak_to_strong",
                   f"弱转强买入(下杀上钩): 入选 {trade_date}"
                   + (f" · 最低跌 {dipped:+.2f}% → 现价回升 {recovered:+.2f}%" if dipped is not None and recovered is not None else ""))
