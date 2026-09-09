# -*- coding: utf-8 -*-
# 免费公网行情源 (新浪日K + 腾讯实时) -- 不依赖 QMT / miniQMT 权限
"""
背景:
    本机国金账号 m_nLoginMiniQmt=0 (未开通极简模式), xtdata / xttrader 外部接口
    全部连不上; QMT 编辑器策略桥又受沙箱限制. 本模块用两个免鉴权公网接口兜底:

    - 日 K:  新浪 CN_MarketDataService.getKLineData  (含 ETF, 覆盖到最近交易日)
    - 实时:  腾讯 qt.gtimg.cn                        (含 ETF, 当日最新价 / 五档)

代码格式: 本系统统一用 '600519.SH' / '002241.SZ' / '513100.SH';
          这里转成新浪/腾讯的 'sh600519' / 'sz002241' 形式.

全部 fail-soft: 网络不通时抛 FreeMarketError, 上层回退到其它数据源.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Dict, List, Optional

import pandas as pd

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TIMEOUT = 10.0


class FreeMarketError(RuntimeError):
    pass


def to_sina_symbol(code: str) -> str:
    """'600519.SH' -> 'sh600519'; 已带前缀的原样小写返回."""
    c = (code or "").strip().upper()
    if "." in c:
        num, mkt = c.split(".", 1)
        mkt = mkt.split(" ")[0]
        if mkt in ("SH", "SS"):
            return "sh" + num
        if mkt == "SZ":
            return "sz" + num
        if mkt in ("BJ",):
            return "bj" + num
        return "sh" + num
    if c[:2].lower() in ("sh", "sz", "bj"):
        return c.lower()
    # 裸 6 位: 6/5/9 开头归沪, 其余归深
    return ("sh" if c[0] in "569" else "sz") + c


def _http_get(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as err:
        raise FreeMarketError(f"请求失败: {err}") from err
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


# ============================================================
# 日 K (新浪)
# ============================================================

def load_daily_kline(stock_code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     datalen: int = 500) -> pd.DataFrame:
    """日 K -> DatetimeIndex + open/high/low/close/volume (与 backtest_data 同格式)"""
    return _load_sina_kline(stock_code, scale=240, start_date=start_date,
                            end_date=end_date, datalen=datalen)


def load_intraday_kline(stock_code: str, period: str = "5m",
                        datalen: int = 300) -> pd.DataFrame:
    """分钟 K ('1m'/'5m'/'15m'/'30m'/'60m') -> DatetimeIndex + OHLCV"""
    scale = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(
        (period or "5m").lower(), 5)
    return _load_sina_kline(stock_code, scale=scale, datalen=datalen)


def _load_sina_kline(stock_code: str, scale: int,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     datalen: int = 500) -> pd.DataFrame:
    sym = to_sina_symbol(stock_code)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           f"CN_MarketDataService.getKLineData?symbol={sym}"
           f"&scale={scale}&ma=no&datalen={datalen}")
    text = _http_get(url)
    try:
        rows = json.loads(text)
    except Exception as err:
        raise FreeMarketError(f"{stock_code} 新浪返回解析失败") from err
    if not rows:
        raise FreeMarketError(f"新浪无数据: {stock_code}")

    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["day"], errors="coerce")
    df = df.set_index("day").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]

    s = (start_date or "").replace("-", "")[:8]
    e = (end_date or "").replace("-", "")[:8]
    if s:
        df = df[df.index >= pd.to_datetime(s, format="%Y%m%d", errors="coerce")]
    if e:
        df = df[df.index <= pd.to_datetime(e, format="%Y%m%d", errors="coerce")]
    if df.empty:
        raise FreeMarketError(f"新浪数据区间为空: {stock_code}")
    return df[["open", "high", "low", "close", "volume"]]


# ============================================================
# 实时盘口 (腾讯)
# ============================================================

def get_latest_ticks(codes: List[str]) -> Dict[str, dict]:
    """实时价 -> {code: {name, lastPrice, open, high, low, prevClose, time}}"""
    if not codes:
        return {}
    syms = ",".join(to_sina_symbol(c) for c in codes)
    text = _http_get(f"http://qt.gtimg.cn/q={syms}")
    out: Dict[str, dict] = {}
    for line in text.strip().splitlines():
        if "=" not in line:
            continue
        var = line.split("=", 1)[0].strip()          # v_sh600519
        sina_sym = var.replace("v_", "")
        if sina_sym[:2].lower() not in ("sh", "sz", "bj"):
            continue
        num = sina_sym[2:]
        code = num + (".SH" if sina_sym[:2].lower() == "sh" else
                      ".SZ" if sina_sym[:2].lower() == "sz" else ".BJ")
        f = line.split("~")
        if len(f) < 46 or not f[3]:
            continue
        try:
            tick = {
                "name": f[1],
                "lastPrice": float(f[3]),
                "prevClose": float(f[4]) if f[4] else None,
                "open": float(f[5]) if f[5] else None,
                "high": float(f[33]) if f[33] else None,
                "low": float(f[34]) if f[34] else None,
                "time": f[30],
            }
        except (ValueError, IndexError):
            continue
        out[code] = tick
    if not out:
        raise FreeMarketError("腾讯实时无返回")
    return out


def get_latest_close(stock_code: str) -> Optional[float]:
    """单只最新价 (盘中=现价, 收盘后=收盘价); 拿不到返回 None."""
    try:
        t = get_latest_ticks([stock_code])
        tick = t.get(stock_code)
        return float(tick["lastPrice"]) if tick and tick.get("lastPrice") else None
    except Exception:
        return None
