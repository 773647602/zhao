# -*- coding: utf-8 -*-
"""
日线K线数据入库脚本 —— 国金证券大QMT 版

通过 bigqmt ZMQ RPC 桥从国金大QMT 拉取【全A股】日K，
幂等写入 MySQL 表 trade_stock_daily（唯一键 stock_code + trade_date）。

范围：默认 2026-08-15 ~ 2026-09-07（含）。
用法：
    python 日线数据-国金QMT入库.py
    python 日线数据-国金QMT入库.py --start 20260815 --end 20260907 --only 600519.SH,000001.SZ
"""
import os
import sys
import time
import argparse
import datetime

os.environ.setdefault("BIGQMT_ACCOUNT_ID", "8890809055")
os.environ.setdefault("BIGQMT_RPC_TRANSPORT", "zmq")
os.environ.setdefault("BIGQMT_RPC_TIMEOUT_SECONDS", "120")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
from lib.paths import ENV_FILE
load_dotenv(ENV_FILE)

import pymysql
import pandas as pd
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, get_default_client
from lib.free_market import load_daily_kline
from lib.ingest_progress import report

_SCRIPT_NAME = "日线数据-国金QMT入库.py"

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
CREATE TABLE IF NOT EXISTS `trade_stock_daily` (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stock_code VARCHAR(12) NOT NULL COMMENT '代码, 如 600519.SH',
    trade_date DATE NOT NULL COMMENT '交易日',
    open_price DECIMAL(12,4) DEFAULT NULL,
    high_price DECIMAL(12,4) DEFAULT NULL,
    low_price  DECIMAL(12,4) DEFAULT NULL,
    close_price DECIMAL(12,4) DEFAULT NULL,
    volume     BIGINT DEFAULT NULL COMMENT '成交量(手)',
    amount     DECIMAL(18,4) DEFAULT NULL COMMENT '成交额(元)',
    PRIMARY KEY (id),
    UNIQUE KEY `uk_code_date` (`stock_code`, `trade_date`),
    KEY `idx_code` (`stock_code`),
    KEY `idx_date` (`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_schema():
    conn = pymysql.connect(host=DB_CFG["host"], port=DB_CFG["port"],
                           user=DB_CFG["user"], password=DB_CFG["password"], charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute(DDL_DB % DB_CFG["database"])
        conn.select_db(DB_CFG["database"])
        cur.execute(DDL_TABLE)
        conn.commit(); cur.close()
        print(f"[DB] 就绪: {DB_CFG['database']}.trade_stock_daily")
    finally:
        conn.close()


def pd_date(v):
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.date() if isinstance(v, datetime.datetime) else v
    if isinstance(v, pd.Timestamp):
        return v.date()
    s = str(v)
    s = s.split(" ")[0]
    s = s.replace("-", "")
    try:
        return datetime.datetime.strptime(s[:8], "%Y%m%d").date()
    except Exception:
        return None


def _is_placeholder(row):
    """判定占位行: 成交量为空 或 价格异常(<=0).
    一字板(open=close 但 volume>0) 是真实行情, 不算占位."""
    try:
        if float(row.get("volume") or 0) <= 0:
            return True
    except Exception:
        return True
    for k in ("open", "high", "low", "close"):
        v = row.get(k)
        if v is None:
            continue  # 字段缺失不算占位
        try:
            if float(v) <= 0:
                return True
        except Exception:
            return True
    return False


def _sina_fix(codes_dates, batch, flush, sleep_s=0.3, datalen_base=10):
    """用新浪直连补真实日线(参考 fix_新浪9月7日.py).
    新浪 volume 单位是股 -> 存库转手(÷100). 真停牌新浪无当日bar, 保留占位."""
    ok = fail = 0
    today = datetime.date.today()
    for code, d in codes_dates:
        try:
            dl = max(datalen_base, (today - d).days + 10)
            df = load_daily_kline(code, datalen=dl)
            df = df[df.index <= pd.Timestamp(d)]
            if df.empty:
                fail += 1
            else:
                r = df.iloc[-1]
                batch.append((code, d,
                              round(float(r["open"]), 4), round(float(r["high"]), 4),
                              round(float(r["low"]), 4), round(float(r["close"]), 4),
                              int(r["volume"] // 100), None))
                ok += 1
                if len(batch) >= 100:
                    flush()
        except Exception:
            fail += 1
        time.sleep(sleep_s)
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260815")
    ap.add_argument("--end", default="20260908")  # 截止日次日, 确保包含当天
    ap.add_argument("--only", default="")
    ap.add_argument("--fix-sleep", type=float, default=0.3,
                    help="新浪补数每只间隔秒, 防限流")
    ap.add_argument("--chunk", type=int, default=100,
                    help="每批拉取股票数(快通道公式服务), 默认100")
    ap.add_argument("--increment", action="store_true",
                    help="每日增量模式: 只拉当日(--date, 默认今天)未入库股票, 占位/价格异常用新浪修复")
    ap.add_argument("--gap-fill", action="store_true",
                    help="历史补齐模式: 对区间内全部股票拉取, 按(stock_code,trade_date)幂等补齐缺失日期"
                         "(不按股票整只跳过, upsert 覆盖已有日期为同值)")
    ap.add_argument("--date", default="",
                    help="增量模式指定日期 YYYYMMDD, 默认今天")
    args = ap.parse_args()

    if args.increment:
        d = (datetime.datetime.strptime(args.date, "%Y%m%d").date()
             if args.date else datetime.date.today())
        d_lo = d_end = d
        pull_end = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")
        fetch_start = d.strftime("%Y%m%d")
        fetch_end = pull_end
    else:
        d_lo = datetime.datetime.strptime(args.start, "%Y%m%d").date()
        d_end = datetime.datetime.strptime(args.end, "%Y%m%d").date()
        pull_end = (d_end + datetime.timedelta(days=1)).strftime("%Y%m%d")
        fetch_start = args.start
        fetch_end = pull_end

    ensure_schema()
    xtdata = BigQmtXtData(get_default_client())

    if args.only:
        codes = [c.strip() for c in args.only.split(",") if c.strip()]
    else:
        codes = xtdata.get_stock_list_in_sector("沪深A股")
    print(f"[INIT] 股票数: {len(codes)}  范围: {d_lo} ~ {d_end}")

    conn = pymysql.connect(**DB_CFG); conn.autocommit(False)
    cur = conn.cursor()
    if args.increment:
        # 增量: 只跳过"当日已有数据"的股票, 保证当日新数据能被拉到
        cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily "
                    "WHERE trade_date=%s", (d,))
    else:
        cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily "
                    "WHERE trade_date>=%s AND trade_date<%s", (d_lo, pull_end))
    done_set = {r[0] for r in cur.fetchall()}

    ok = skip = fail = 0
    batch = []
    placeholder = []

    def flush():
        if not batch:
            return
        cur.executemany(
            "INSERT INTO trade_stock_daily "
            "(stock_code, trade_date, open_price, high_price, low_price, close_price, volume, amount) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "open_price=VALUES(open_price),high_price=VALUES(high_price),low_price=VALUES(low_price),"
            "close_price=VALUES(close_price),volume=VALUES(volume),amount=VALUES(amount)",
            batch)
        conn.commit(); batch.clear()

    def _n(v):
        try:
            f = float(v)
            return None if f != f else f
        except Exception:
            return None

    # 过滤掉已入库股票 (gap-fill 时按日粒度幂等补齐, 不整只跳过)
    todo = codes if args.gap_fill else [c for c in codes if c not in done_set]
    print(f"[INIT] 待处理: {len(todo)} 只 (已完成 {len(done_set)})")
    report(_SCRIPT_NAME, "初始化", 0, len(todo), f"待处理 {len(todo)} 只", "running")

    chunk = args.chunk or 100
    t0 = time.time()
    for bi in range(0, len(todo), chunk):
        sub = todo[bi:bi + chunk]
        try:
            res = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=sub, period="1d",
                start_time=fetch_start, end_time=fetch_end,
                count=-1, dividend_type="front", fill_data=False,
                use_formula=True)
        except Exception as e:
            print(f"[批次{bi//chunk}] 拉取 {len(sub)} 只异常: {type(e).__name__}: {e}")
            fail += len(sub)
            continue

        if not isinstance(res, dict):
            fail += len(sub)
            print(f"[批次{bi//chunk}] 返回非dict, 跳过")
            continue

        for code in sub:
            df = res.get(code)
            if not hasattr(df, "itertuples"):
                fail += 1
                continue
            n = 0
            for row in df.itertuples(index=True):
                rd = row._asdict()
                td = pd_date(rd.pop("Index", None) or rd.get("index") or None)
                if td is None:
                    continue
                if d_lo <= td <= d_end:
                    if _is_placeholder(rd):
                        placeholder.append((code, td))
                        continue
                    batch.append((code, td, _n(rd.get("open")), _n(rd.get("high")),
                                  _n(rd.get("low")), _n(rd.get("close")),
                                  _n(rd.get("volume")), _n(rd.get("amount"))))
                    n += 1
            if n:
                ok += 1; done_set.add(code)
            else:
                fail += 1

        flush()
        el = time.time() - t0
        done = min(bi + chunk, len(todo))
        print(f"[进度] {done}/{len(todo)} ok={ok} skip={skip} fail={fail} "
              f"用时={el:.0f}s 剩≈{(el/max(done,1))*(len(todo)-done):.0f}s")
        report(_SCRIPT_NAME, "拉取日K", done, len(todo),
               f"ok={ok} skip={skip} fail={fail}", "running")
    flush()
    if placeholder:
        uniq = sorted(set(placeholder))
        print(f"[SINA] 检测到占位 {len(placeholder)} 行(去重 {len(uniq)} 条), 开始新浪补数"
              f" 预计 {len(uniq)*args.fix_sleep:.0f}s", flush=True)
        s_ok, s_fail = _sina_fix(uniq, batch, flush, sleep_s=args.fix_sleep)
        print(f"[SINA] 补数完成 ok={s_ok} fail={s_fail}", flush=True)
    if args.increment:
        # 兜底: 扫描"当日已入库"的占位/异常行, 用新浪修复(大QMT拉取不到时也能修)
        cur.execute("SELECT DISTINCT stock_code FROM trade_stock_daily "
                    "WHERE trade_date=%s AND (volume IS NULL OR volume<=0 OR open_price<=0)", (d,))
        db_ph = [(r[0], d) for r in cur.fetchall()]
        if db_ph:
            print(f"[SINA] 库内当日占位/异常 {len(db_ph)} 条, 新浪修复...", flush=True)
            s_ok2, s_fail2 = _sina_fix(db_ph, batch, flush, sleep_s=args.fix_sleep)
            print(f"[SINA] 库内占位修复 ok={s_ok2} fail={s_fail2}", flush=True)
    flush()
    cur.close(); conn.close()
    el = time.time() - t0
    report(_SCRIPT_NAME, "完成", len(todo), len(todo),
           f"ok={ok} skip={skip} fail={fail} 占位={len(placeholder)} 总耗时={el:.0f}s", "done")
    print(f"\n[DONE] 共{len(codes)}只: ok={ok} skip={skip} fail={fail} "
          f"占位={len(placeholder)} 总耗时={el:.0f}s")


if __name__ == "__main__":
    main()