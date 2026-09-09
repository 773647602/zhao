# -*- coding: utf-8 -*-
"""
分钟线(1分钟K线)数据入库脚本 —— 国金证券大QMT 版

通过 bigqmt ZMQ RPC 桥从国金大QMT 拉取【全A股】1分钟K线，
幂等写入 MySQL 表 trade_stock_minute（唯一键 stock_code + trade_time）。

范围：默认最近10个自然日(含今天)。发现重复数据会覆盖更新(幂等)。
用法：
    python 分钟数据-国金QMT入库.py                      # 默认回填最近10天含今天
    python 分钟数据-国金QMT入库.py --end 20260908       # 指定截止日
    python 分钟数据-国金QMT入库.py --only 600519.SH --start 20260908 --end 20260908  # 单只补数

前置：
    1. 国金QMT 已登录并运行 BIGQMT 桥(端口15615)
    2. 数据库可连；脚本自动创建 wucai_trade 库 + trade_stock_minute 表
"""
import os
import sys
import time
import argparse
import datetime

# ---- 国金大QMT 账号 / 传输类型（桥必须已在 15615 监听），import 前设置 ----
os.environ.setdefault("BIGQMT_ACCOUNT_ID", "8890809055")
os.environ.setdefault("BIGQMT_RPC_TRANSPORT", "zmq")
os.environ.setdefault("BIGQMT_RPC_TIMEOUT_SECONDS", "120")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)

import pymysql
import pandas as pd
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, get_default_client
from lib.ingest_progress import report

_SCRIPT_NAME = "分钟数据-国金QMT入库.py"

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
CREATE TABLE IF NOT EXISTS `trade_stock_minute` (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stock_code VARCHAR(12) NOT NULL COMMENT '代码, 如 600519.SH',
    trade_time DATETIME NOT NULL COMMENT '分钟时间',
    open_price DECIMAL(12,4) DEFAULT NULL,
    high_price DECIMAL(12,4) DEFAULT NULL,
    low_price  DECIMAL(12,4) DEFAULT NULL,
    close_price DECIMAL(12,4) DEFAULT NULL,
    amount     DECIMAL(18,4) DEFAULT NULL COMMENT '成交额(元)',
    PRIMARY KEY (id),
    UNIQUE KEY `uk_code_time` (`stock_code`, `trade_time`),
    KEY `idx_code` (`stock_code`),
    KEY `idx_time` (`trade_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_schema():
    conn = pymysql.connect(host=DB_CFG["host"], port=DB_CFG["port"],
                           user=DB_CFG["user"], password=DB_CFG["password"],
                           charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute(DDL_DB % DB_CFG["database"])
        conn.select_db(DB_CFG["database"])
        cur.execute(DDL_TABLE)
        conn.commit()
        cur.close()
        print(f"[DB] 就绪: {DB_CFG['database']}.trade_stock_minute")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="", help="起始日 YYYYMMDD (默认=结束日前10个自然日)")
    ap.add_argument("--end", default="", help="结束日 YYYYMMDD (默认=今天), 传下一天更稳")
    ap.add_argument("--only", default="", help="只处理指定代码(逗号分隔)，用于补数/调试")
    ap.add_argument("--chunk", type=int, default=100, help="每批拉取股票数(快通道公式服务), 默认100, 失败自动逐只回退")
    args = ap.parse_args()

    start, end = args.start, args.end
    # 默认: end=今天, start=end前10个自然日(回填最近10个交易日, 兼容断点续跑)
    if not end:
        end = datetime.date.today().strftime("%Y%m%d")
    if not start:
        d_end_default = datetime.datetime.strptime(end, "%Y%m%d").date()
        start = (d_end_default - datetime.timedelta(days=10)).strftime("%Y%m%d")
    # 结果时间过滤区间（不含结束当天的 end 本身；end 作为 K 线拉取的含当天界）
    d_lo = datetime.datetime.strptime(start, "%Y%m%d").date()
    d_end = datetime.datetime.strptime(end, "%Y%m%d").date()  # 用户意图的截止日
    pull_end = (d_end + datetime.timedelta(days=1)).strftime("%Y%m%d")  # 拉到截止日次日, 确保包含当天

    ensure_schema()
    xtdata = BigQmtXtData(get_default_client())

    if args.only:
        codes = [c.strip() for c in args.only.split(",") if c.strip()]
    else:
        codes = xtdata.get_stock_list_in_sector("沪深A股")
    print(f"[INIT] 股票数: {len(codes)}  范围: {d_lo} ~ {d_end}")

    # 连接库
    conn = pymysql.connect(**DB_CFG)
    conn.autocommit(False)
    cur = conn.cursor()
    # 已入库股票集合（用于断点续跑：该股在区间内已有数据则跳过）
    cur.execute("SELECT DISTINCT stock_code FROM trade_stock_minute "
                "WHERE trade_time>=%s AND trade_time<%s", (d_lo, pull_end))
    done_set = {r[0] for r in cur.fetchall()}

    # 过滤掉已入库股票
    todo = [c for c in codes if c not in done_set]
    print(f"[INIT] 待处理: {len(todo)} 只 (已完成 {len(done_set)})")
    report(_SCRIPT_NAME, "初始化", 0, len(todo), f"待处理 {len(todo)} 只", "running")

    ok, skip, fail = 0, 0, 0
    batch_rows = []
    flush_every = 20000

    def flush():
        nonlocal batch_rows
        if not batch_rows:
            return
        cur.executemany(
            "INSERT INTO trade_stock_minute "
            "(stock_code, trade_time, open_price, high_price, low_price, close_price, amount) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE open_price=VALUES(open_price), high_price=VALUES(high_price), "
            "low_price=VALUES(low_price), close_price=VALUES(close_price), amount=VALUES(amount)",
            batch_rows)
        conn.commit()
        batch_rows = []
    def _n(v):
        try:
            f = float(v)
            return None if f != f else f  # NaN -> None
        except Exception:
            return None

    t0 = time.time()
    def pull(codes):
        """快通道(大QMT公式服务)拉分钟K; 失败逐只回退. 不取volume字段."""
        try:
            res = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "amount"],
                stock_list=codes, period="1m",
                start_time=start, end_time=pull_end,
                count=-1, dividend_type="front", fill_data=False,
                use_formula=True)
            return res
        except Exception as e:
            print(f"[拉取] {len(codes)} 只异常: {type(e).__name__}: {e}")
            return None

    def ingest(code, df):
        """解析一只的分钟K, 返回有效行数. 占位/异常行跳过, 不写库.
        占位识别: 成交额=0 (QMT假数据常量价+零量) 或 价格非法(<=0)."""
        nonlocal batch_rows
        n = 0
        if not hasattr(df, "itertuples"):
            return 0
        for ts, d in _iter_parsed(df, d_lo, d_end):
            amt = _n(d.get("amount"))
            if amt in (None, 0) or _n(d.get("open")) in (None, 0) or _n(d.get("close")) in (None, 0):
                continue  # 占位(无量)/异常(价非法) 分钟, 跳过防污染
            batch_rows.append((code, ts, _n(d.get("open")), _n(d.get("high")),
                               _n(d.get("low")), _n(d.get("close")), amt))
            n += 1
        return n

    chunk = args.chunk or 50  # 快通道可批量, 失败即时逐只回退
    for bi in range(0, len(todo), chunk):
        sub = todo[bi:bi + chunk]
        res = pull(sub)
        if isinstance(res, dict):
            for code in sub:
                df = res.get(code)
                n_parsed = ingest(code, df) if hasattr(df, "itertuples") else 0
                if n_parsed:
                    ok += 1
                    done_set.add(code)
                else:
                    fail += 1
            flush()
        else:
            # 批量失败 -> 逐只回退
            for code in sub:
                df = pull([code])
                n_parsed = ingest(code, df.get(code)) if isinstance(df, dict) else 0
                if n_parsed:
                    ok += 1
                else:
                    fail += 1
                    print(f"  [回退] {code} 仍无有效分钟数据")
                flush()
        el = time.time() - t0
        done = min(bi + chunk, len(todo))
        print(f"[进度] {done}/{len(todo)} ok={ok} fail={fail} rows={len(batch_rows)//1000}k "
              f"用时={el:.0f}s 均速={done/max(el,1):.1f}/s 剩≈{(el/max(done,1))*(len(todo)-done):.0f}s")
        report(_SCRIPT_NAME, "拉取分钟K", done, len(todo),
               f"ok={ok} fail={fail} 用时={el:.0f}s", "running")

    flush()
    cur.close()
    conn.close()
    el = time.time() - t0
    report(_SCRIPT_NAME, "完成", len(todo), len(todo),
           f"ok={ok} skip={skip} fail={fail} 总耗时={el:.0f}s", "done")
    print(f"\n[DONE] 共{len(codes)}只: ok={ok} skip={skip} fail={fail} 总耗时={el:.0f}s")


def pd_ts(v):
    if v is None:
        return None
    s = str(v)
    s = s.split(".")[0].split(" ")[0]  # 去毫秒/时间部分
    s = s.replace("-", "").replace(":", "").replace(" ", "")[:14]
    try:
        return datetime.datetime.strptime(s, "%Y%m%d%H%M%S")
    except Exception:
        pass
    try:
        return pd.Timestamp(float(v)).to_pydatetime()
    except Exception:
        return None


def _iter_parsed(df, d_lo, d_end):
    """快通道: 时间戳在 DataFrame 索引(14位串). 慢通道: 在 index 列.
    兼容两者, 产出 (datetime, 行dict)."""
    for row in df.itertuples(index=True):
        rd = row._asdict()
        ts = pd_ts(rd.pop("Index", None) or rd.get("index") or None)
        if ts is None:
            continue
        if d_lo <= ts.date() <= d_end:
            yield ts, rd


if __name__ == "__main__":
    main()