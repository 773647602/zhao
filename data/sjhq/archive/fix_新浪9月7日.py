# -*- coding: utf-8 -*-
"""修复 9/7 日线占位行: 用新浪直连(免费/稳定) 拉取真实行情, 覆盖 trade_stock_daily.
volume: 新浪为股 -> 存库转手(÷100). 仅覆盖目标交易日占位行, 不破坏已有真实数据."""
import os, sys, io, time, argparse, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)
import pymysql, pandas as pd
from lib.free_market import load_daily_kline

DB_CFG = {
    "host": os.environ.get("WUCAI_SQL_HOST", "localhost"),
    "port": int(os.environ.get("WUCAI_SQL_PORT", "3306")),
    "user": os.environ.get("WUCAI_SQL_USERNAME", "root"),
    "password": os.environ.get("WUCAI_SQL_PASSWORD", ""),
    "database": os.environ.get("WUCAI_SQL_DB", "wucai_trade"),
    "charset": "utf8mb4",
}

ap = argparse.ArgumentParser()
ap.add_argument("--date", default="20260907")
ap.add_argument("--limit", type=int, default=0)      # 0=全部占位; >0 仅处理前N只
ap.add_argument("--only", default="")
ap.add_argument("--sleep", type=float, default=0.3)  # 每只间隔, 防限流
args = ap.parse_args()
d = datetime.datetime.strptime(args.date, "%Y%m%d").date()
d_s = args.date

conn = pymysql.connect(**DB_CFG); conn.autocommit(False); cur = conn.cursor()
if args.only:
    codes = [c.strip() for c in args.only.split(",") if c.strip()]
else:
    cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily "
                "WHERE trade_date=%s AND (open_price<=0 OR volume=0 OR open_price=close_price)", (d,))
    per_ = [r[0] for r in cur.fetchall()]
    codes = per_[:args.limit] if args.limit > 0 else per_
    # 排除当日真实停牌(新浪也无当日 bar)不影响, 失败按占位保留
print(f"[INIT] date={d} 待修复={len(codes)} 只  数据源=新浪直连")

ok = fail = 0; t0 = time.time()
def flush_rows(lst):
    if not lst: return
    cur.executemany(
        "INSERT INTO trade_stock_daily (stock_code,trade_date,open_price,high_price,low_price,close_price,volume,amount) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "open_price=VALUES(open_price),high_price=VALUES(high_price),low_price=VALUES(low_price),"
        "close_price=VALUES(close_price),volume=VALUES(volume),amount=VALUES(amount)", lst)
    conn.commit()

rows = []
for i, code in enumerate(codes, 1):
    try:
        df = load_daily_kline(code, datalen=8)
        df = df[df.index <= pd.Timestamp(d)]
        if df.empty:
            fail += 1
        else:
            r = df.iloc[-1]
            # 数量转手: 新浪 volume 是股
            rows.append((code, d, round(float(r["open"]),4), round(float(r["high"]),4),
                         round(float(r["low"]),4), round(float(r["close"]),4),
                         int(r["volume"]//100), None))
            ok += 1
    except Exception as e:
        fail += 1
        if i <= 5 or fail % 500 == 0:
            print(f"  [{code}] 失败: {type(e).__name__}: {e}", flush=True)
    if len(rows) >= 100:
        flush_rows(rows); rows = []
    if i % 200 == 0:
        el = time.time()-t0
        print(f"[进度] {i}/{len(codes)} ok={ok} fail={fail} 用时{el:.0f}s "
              f"剩约{el/max(i,1)*(len(codes)-i):.0f}s", flush=True)
    time.sleep(args.sleep)
flush_rows(rows)
print(f"[DONE] 9/7 修复 ok={ok} fail={fail} 总耗时{time.time()-t0:.0f}s")
conn.close()