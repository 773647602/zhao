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


# ============================================================
# 全市场量能 (新浪指数接口)
# ============================================================

# 上证指数 / 深证综指: 分别代表沪市、深市整体成交; 两者相加 ≈ 沪深两市总成交
_SINA_INDEX_SYMS = ("s_sh000001", "s_sz399106")


def get_market_total_amount() -> Dict[str, float]:
    """当日沪深两市成交总额(元) -> {sh, sz, total}

    数据源: 新浪 hq.sinajs.cn 指数接口 s_sh000001(上证) / s_sz399106(深证综指)。
    返回字段: 名称,当前点数,涨跌额,涨跌幅,成交量(手),成交额(万元)。
    取两市成交额(万元)换算为元; fail-soft: 请求/解析失败抛 FreeMarketError。
    """
    url = "https://hq.sinajs.cn/list=" + ",".join(_SINA_INDEX_SYMS)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA.get("User-Agent", "Mozilla/5.0"),
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as err:
        raise FreeMarketError(f"新浪指数请求失败: {err}") from err
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        text = raw.decode("utf-8", "ignore")

    amounts: Dict[str, float] = {"sh": 0.0, "sz": 0.0}
    count = 0
    for line in text.strip().splitlines():
        if "=" not in line:
            continue
        var = line.split("=", 1)[0].strip()          # var hq_str_s_sh000001
        sym = var.replace("var hq_str_", "").strip()
        if sym not in _SINA_INDEX_SYMS:
            continue
        body = line.split('="', 1)[1].rsplit('";', 1)[0].strip()
        fields = body.split(",")
        # [名称, 点数, 涨跌, 涨跌幅, 成交量(手), 成交额(万元)]
        if len(fields) < 6 or not fields[5]:
            continue
        try:
            amt_wan = float(fields[5])
        except ValueError:
            continue
        if sym == "s_sh000001":
            amounts["sh"] = amt_wan * 10000.0
        elif sym == "s_sz399106":
            amounts["sz"] = amt_wan * 10000.0
        count += 1

    if count < 2:
        raise FreeMarketError("新浪指数无完整返回 (两市成交额)")
    amounts["total"] = amounts["sh"] + amounts["sz"]
    return amounts


# ============================================================
# 历史量能 (东财指数K线) -- 用于回补历史交易日
# ============================================================

# 东财 secid: 上证指数 1.000001 / 深证综指 0.399106 (与新浪指数口径一致)
_EM_INDEX_SECIDS = (("1.000001", "sh"), ("0.399106", "sz"))


def get_market_total_amount_by_date(trade_date: str) -> Dict[str, float]:
    """指定交易日两市成交总额(元) -> {sh, sz, total}

    数据源: 东财 push2his 指数K线(上证指数/深证综指), amount 字段为当日成交额(元)。
    用于回补历史日期, 与实时新浪口径一致(已校验 09-10 两源数值相同)。
    trade_date: 格式 'YYYY-MM-DD' 或 'YYYYMMDD'; 非交易日或数据缺失抛 FreeMarketError。
    """
    d = (trade_date or "").replace("-", "")
    if len(d) != 8:
        raise FreeMarketError(f"日期格式错误: {trade_date}")

    amounts: Dict[str, float] = {"sh": 0.0, "sz": 0.0}
    count = 0
    for secid, key in _EM_INDEX_SECIDS:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            f"secid={secid}&klt=101&fqt=1"
            "&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            "&beg=20260901&end=20260910"
        )
        # 东财对单日/过小区间偶发断连, 固定拉取整段区间再本地过滤目标日, 稳定可靠
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
            payload = json.loads(raw.decode("utf-8"))
        except Exception as err:
            raise FreeMarketError(f"东财指数K线失败 {secid}: {err}") from err

        klines = (payload.get("data") or {}).get("klines") or []
        # kline 列: 日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
        matched = None
        for line in klines:
            f = line.split(",")
            if len(f) >= 7 and f[0].replace("/", "-").replace("'", "")[:8] == d:
                matched = f
                break
        if matched is None:
            raise FreeMarketError(f"东财无 {trade_date} 指数K线: {secid}")
        try:
            amt = float(matched[6])
        except (ValueError, IndexError):
            raise FreeMarketError(f"东财 {trade_date} 成交额解析失败: {secid}")
        amounts[key] = amt
        count += 1

    if count < 2 or amounts["sh"] <= 0 or amounts["sz"] <= 0:
        raise FreeMarketError(f"东财 {trade_date} 两市成交额数据不完整")
    amounts["total"] = amounts["sh"] + amounts["sz"]
    return amounts
