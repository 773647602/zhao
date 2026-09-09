# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"d:\CASE-AI量化系统")
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)
import pymysql
from lib.backtest_data import _db_config
conn = pymysql.connect(**_db_config())
cur = conn.cursor()
cur.execute("""
  SELECT trade_date, close_price FROM trade_stock_daily
  WHERE stock_code='300562.SZ' AND trade_date BETWEEN '2026-08-26' AND '2026-09-07'
  ORDER BY trade_date
""")
rows = cur.fetchall()
closes = [r[1] for r in rows]
ma5 = sum(closes[-6:-1])/5 if len(closes) >= 6 else None
for d, c in rows:
    print(d, c)
print("昨收(09-04) =", rows[-2][1], " 5日线(09-04前5日) =", round(ma5, 3) if ma5 else None)
cur.close(); conn.close()
