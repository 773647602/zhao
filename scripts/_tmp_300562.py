# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"d:\CASE-AI量化系统")
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)
from lib.selection_engine import build_universe_snapshot

snap = build_universe_snapshot(asof_date="2026-09-07")
meta = snap.meta
if "300562.SZ" in meta.index:
    r = meta.loc["300562.SZ"]
    def f(col):
        v = r.get(col)
        return None if v is None or (hasattr(v, "isna") and v.isna()) else float(v)
    print(f"股票: 300562.SZ  (快照最新日 {snap.latest_date})")
    print(f"  昨收涨幅 prev_pct       = {f('prev_pct')}%   (需 < 0%)")
    print(f"  今日开盘涨幅 open_pct   = {f('open_pct')}%   (需 > 2%)")
    print(f"  昨量较前日 vol_grow_pct = {f('vol_grow_pct')}%   (需 >= 1%)")
    print(f"  昨收 above_ma5_prev    = {f('above_ma5_prev')}   (需 1=站上5日线)")
    print(f"  ma5_prev = {f('ma5_prev')}  昨收 prev_close = {f('prev_close')}")
    print(f"  当日涨跌幅 pct_change   = {f('pct_change')}%")
else:
    print("300562.SZ 不在快照中 (无09-07日线数据?)")
