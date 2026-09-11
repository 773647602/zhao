# -*- coding: utf-8 -*-
# 选股池面板配置 -- 09:28 推监控池任务的可调参数
"""
存放选股池界面可配置的参数, 持久化到 config/pool_config.yaml。

当前两项 (均供 app.py 09:28 推送监控池任务用):
    min_market_yi     -- 昨日全市场量能阈值 (单位: 万亿), 默认 2.0
                         昨日量能 > 阈值   -> 推「当日」选股池候选;
                         昨日量能 <= 阈值  -> 推「昨日」候选里收盘价 < 开盘价的阴线股。
    prev_drop_min_pct -- 昨日阴线候选的昨日涨幅下限 (%), 默认 -5.0
                         量能不足分支里, 仅昨日涨幅 > 该值 (跌幅不深) 的阴线候选才入池。
"""
from __future__ import annotations

import yaml

from lib.paths import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "pool_config.yaml"

DEFAULT_MIN_MARKET_YI = 2.0     # 2 万亿
DEFAULT_PREV_DROP_MIN_PCT = -5.0  # 昨日涨幅 > -5%

_HEADER = (
    "# 选股池面板配置 (可在选股池界面修改)\n"
    "# min_market_yi: 昨日全市场量能阈值(万亿)。推送监控池判断用:\n"
    "#   昨日量能 > 阈值  -> 推「当日」选股池候选;\n"
    "#   昨日量能 <= 阈值 -> 推「昨日」候选里收盘价 < 开盘价的阴线股。\n"
    "# prev_drop_min_pct: 昨日阴线候选的昨日涨幅下限(%), 默认 -5.0\n"
    "#   量能不足分支里, 仅昨日涨幅 > 该值 (跌幅不深) 的阴线候选才入池。\n"
)


def _read() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set(key: str, val: float) -> float:
    """写入一项配置并落盘, 返回保存后的规范值"""
    data = _read()
    data[key] = val
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    CONFIG_FILE.write_text(_HEADER + body, encoding="utf-8")
    return val


# ---------------- 昨日量能阈值 (万亿) ----------------

def load_min_market_yi() -> float:
    """读取昨日量能阈值(万亿), 缺省/异常回退默认 2.0"""
    try:
        v = _read().get("min_market_yi")
        if v is None:
            return DEFAULT_MIN_MARKET_YI
        return float(v)
    except Exception:
        return DEFAULT_MIN_MARKET_YI


def load_min_market_amount_yuan() -> float:
    """昨日量能阈值换算为元 (供推送任务比较 amount)"""
    return load_min_market_yi() * 1e12


def save_min_market_yi(yi: float) -> float:
    """写入昨日量能阈值(万亿), 返回保存后的值 (非法输入回退默认)。"""
    try:
        v = float(yi)
        if not (0 < v <= 100):
            v = DEFAULT_MIN_MARKET_YI
    except Exception:
        v = DEFAULT_MIN_MARKET_YI
    return _set("min_market_yi", round(v, 2))


# ---------------- 昨日阴线涨幅下限 (%) ----------------

def load_prev_drop_min_pct() -> float:
    """读取昨日阴线候选的昨日涨幅下限(%), 缺省/异常回退默认 -5.0"""
    try:
        v = _read().get("prev_drop_min_pct")
        if v is None:
            return DEFAULT_PREV_DROP_MIN_PCT
        return float(v)
    except Exception:
        return DEFAULT_PREV_DROP_MIN_PCT


def save_prev_drop_min_pct(pct: float) -> float:
    """写入昨日涨幅下限(%), 返回保存后的值 (非法输入回退默认)。"""
    try:
        v = float(pct)
        if not (-15 <= v < 0):
            v = DEFAULT_PREV_DROP_MIN_PCT
    except Exception:
        v = DEFAULT_PREV_DROP_MIN_PCT
    return _set("prev_drop_min_pct", round(v, 2))