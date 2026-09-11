# -*- coding: utf-8 -*-
"""
股票基础信息入库脚本 —— 国金证券大QMT 版

通过 bigqmt ZMQ RPC 桥拉取【全A股】股票名称,
幂等写入 MySQL 表 trade_stock_basic（唯一键 stock_code）。

选股引擎 lib/selection_engine.py::_stock_name_map() 会自动读取本表,
入库后选股理由/选股池即可显示股票名称。

用法:
    python 股票基础信息-国金QMT入库.py
    python 股票基础信息-国金QMT入库.py --only 600519.SH,000001.SZ   # 只刷指定股票
"""
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("BIGQMT_ACCOUNT_ID", "8890809055")
os.environ.setdefault("BIGQMT_RPC_TRANSPORT", "zmq")
os.environ.setdefault("BIGQMT_RPC_TIMEOUT_SECONDS", "120")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)

import pymysql
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, get_default_client

DB_CFG = {
    "host": os.environ.get("WUCAI_SQL_HOST", "localhost"),
    "port": int(os.environ.get("WUCAI_SQL_PORT", "3306")),
    "user": os.environ.get("WUCAI_SQL_USERNAME", "root"),
    "password": os.environ.get("WUCAI_SQL_PASSWORD", ""),
    "database": os.environ.get("WUCAI_SQL_DB", "wucai_trade"),
    "charset": "utf8mb4",
}
DDL_DB = "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
DDL_TABLE = """
CREATE TABLE IF NOT EXISTS `trade_stock_basic` (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stock_code VARCHAR(12) NOT NULL COMMENT '代码, 如 600519.SH',
    stock_name VARCHAR(64) DEFAULT NULL COMMENT '股票名称, 如 贵州茅台',
    exchange   VARCHAR(8)  DEFAULT NULL COMMENT '交易所: SH / SZ',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近刷新时间',
    PRIMARY KEY (id),
    UNIQUE KEY `uk_code` (`stock_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

BATCH = 500
THREADS = 8


def ensure_schema():
    conn = pymysql.connect(host=DB_CFG["host"], port=DB_CFG["port"],
                           user=DB_CFG["user"], password=DB_CFG["password"], charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute(DDL_DB % DB_CFG["database"])
        conn.select_db(DB_CFG["database"])
        cur.execute(DDL_TABLE)
        conn.commit(); cur.close()
        print(f"[DB] 就绪: {DB_CFG['database']}.trade_stock_basic")
    finally:
        conn.close()


def exchange_of(code: str) -> str:
    return "SH" if code.endswith(".SH") else ("SZ" if code.endswith(".SZ") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只刷新指定股票, 逗号分隔, 如 600519.SH,000001.SZ")
    args = ap.parse_args()

    ensure_schema()

    xt = BigQmtXtData(get_default_client())
    if args.only:
        codes = [c.strip() for c in args.only.split(",") if c.strip()]
    else:
        codes = list(xt.get_stock_list_in_sector("沪深A股"))
    print(f"[QMT] 待刷新 {len(codes)} 只", flush=True)

    t0 = time.time()
    names = {}
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        for code, name in zip(codes, ex.map(xt.get_stock_name, codes)):
            if code and name:
                names[code] = str(name).strip()
    print(f"[QMT] 名称拉取完成 {len(names)}/{len(codes)} 只, 耗时 {time.time()-t0:.0f}s", flush=True)

    rows = [(c, n, exchange_of(c)) for c, n in names.items()]
    conn = pymysql.connect(**DB_CFG)
    try:
        cur = conn.cursor()
        sql = ("INSERT INTO trade_stock_basic (stock_code, stock_name, exchange) VALUES (%s,%s,%s) "
               "ON DUPLICATE KEY UPDATE stock_name=VALUES(stock_name), exchange=VALUES(exchange)")
        for i in range(0, len(rows), BATCH):
            cur.executemany(sql, rows[i:i+BATCH])
            conn.commit()
            done = min(i + BATCH, len(rows))
            print(f"[DB] 写入进度 {done}/{len(rows)}", flush=True)
        cur.close()
    finally:
        conn.close()

    # 校验
    conn = pymysql.connect(**DB_CFG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COUNT(stock_name) FROM trade_stock_basic")
        total, named = cur.fetchone()
        cur.execute("SELECT stock_code, stock_name FROM trade_stock_basic ORDER BY stock_code LIMIT 3")
        samples = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    print(f"[完成] 表内共 {total} 只, 有名称 {named} 只, 示例: {samples}", flush=True)


if __name__ == "__main__":
    main()
