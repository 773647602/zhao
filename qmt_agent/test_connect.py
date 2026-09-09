"""测试 miniQMT 连接 - 带 flush"""
import time
import os
import sys

print("start", flush=True)

from xtquant.xttrader import XtQuantTrader

path = r"D:\光大证券金阳光QMT实盘\userdata_mini"
session = int(time.time()) % 100000

print(f"path  : {path}", flush=True)
print(f"exists: {os.path.exists(path)}", flush=True)
print(f"session: {session}", flush=True)

trader = XtQuantTrader(path, session)
print("calling start()...", flush=True)
trader.start()
print("start() done, sleeping 2s...", flush=True)
time.sleep(2)
print("calling connect()...", flush=True)
result = trader.connect()
print(f"connect result: {result} (0=success)", flush=True)

if result == 0:
    print("连接成功！", flush=True)
    trader.stop()
else:
    print(f"连接失败，code={result}", flush=True)
