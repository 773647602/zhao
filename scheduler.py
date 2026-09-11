# -*- coding: utf-8 -*-
# CASE-AI 量化系统: 定时调度器（实盘时段启停 + 可选数据增量）
"""
TradingScheduler -- 按 A 股交易时段自动启停 +（可选）板块日更脚本

单独进程的原因:
    - Web (app.py) 与调度解耦：浏览器关了不影响调度，调度挂了不影响 Web。
    - APScheduler + cron。

5 个 cron job（时区 Asia/Shanghai）:
    15:00   job_daily_increment   -> 日线增量入库 (大QMT拉最近已收盘交易日 + 新浪修占位/价格异常), 每日执行
    08:30   job_data_refresh      -> .env 中 CASE_A_BOARD_DATA_PREP_DIR/run_daily.py (周一至周五)
09:00   job_start_engine      -> 启动实盘主循环 LiveSimRunner(dry_run=False, 周一至周五)
    14:55   job_stop_engine       -> 停止主循环 (周一至周五)
    15:35   job_minute_increment  -> 分钟线增量入库 (当日1分钟K线, 周一至周五)

主循环状态在 outputs/live_state.json，进程重启可从最近一次 state 恢复。

用法:
    python scheduler.py
    python scheduler.py --simulate
    python scheduler.py --job data | engine | all
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from dotenv import load_dotenv
from lib.paths import ENV_FILE, PROJECT_ROOT, setup_sys_path

load_dotenv(ENV_FILE)

setup_sys_path()

from lib.live_simulator import LiveSimRunner, merge_watch_codes


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("trading-scheduler")


def _case_a_prep_dir() -> Path | None:
    """CASE-A「板块数据准备」目录（内含 run_daily.py），由 .env CASE_A_BOARD_DATA_PREP_DIR 指定."""
    raw = (os.environ.get("CASE_A_BOARD_DATA_PREP_DIR") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


_CASE_A_DIR = _case_a_prep_dir()
CASE_A_RUN_DAILY = (_CASE_A_DIR / "run_daily.py") if _CASE_A_DIR else None


# ============================================================
# 任务
# ============================================================

def job_data_refresh():
    """08:30: 在项目目录下运行 run_daily.py（若未配置 CASE_A_BOARD_DATA_PREP_DIR 则跳过并打日志）"""
    log.info("[JOB] 数据增量 - 触发")
    if not _CASE_A_DIR:
        log.error(
            "未配置环境变量 CASE_A_BOARD_DATA_PREP_DIR（指向内含 run_daily.py 的目录）；已跳过。"
        )
        return
    if not CASE_A_RUN_DAILY or not CASE_A_RUN_DAILY.exists():
        log.error("找不到 run_daily.py: %s", CASE_A_RUN_DAILY)
        return
    ret = subprocess.run([sys.executable, str(CASE_A_RUN_DAILY)], cwd=str(_CASE_A_DIR))
    log.info("[JOB] 数据增量 - 完成 (returncode=%s)", ret.returncode)


def _latest_trade_date() -> str:
    """返回最近一个已收盘交易日 YYYYMMDD。
    15:00 收盘前(如凌晨02:00): 取昨日并回退到最近工作日; 收盘后(如晚间20:00): 取当日, 若为周末回退到周五。
    节假日由脚本空跑兜底。"""
    now = datetime.now()
    d = now.date()
    if now.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # 周六=5 / 周日=6
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def job_daily_increment():
    """15:00: 每日日线增量入库 -- 大QMT拉取最近已收盘交易日未入库日线, 占位/价格异常用新浪修复"""
    log.info("[JOB] 日线增量 - 触发")
    script = PROJECT_ROOT / "data" / "sjhq" / "日线数据-国金QMT入库.py"
    target = _latest_trade_date()
    ret = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), "--increment", "--date", target],
        cwd=str(PROJECT_ROOT),
    )
    log.info("[JOB] 日线增量 - 完成 (returncode=%s, target=%s)", ret.returncode, target)
    # 日线入库后: 选股池「当日涨幅」按入选当日收盘涨幅补齐 (含历史行纠错)
    try:
        from lib.selection_store import backfill_selection_cur_pct
        n = backfill_selection_cur_pct()
        log.info("[JOB] 选股池当日涨幅(收盘)补齐 - 完成 (更新 %s 行)", n)
    except Exception as e:
        log.error("[JOB] 选股池当日涨幅(收盘)补齐失败: %s", e)


def job_minute_increment():
    """15:35: 每日分钟线增量入库 -- 大QMT拉取当日1分钟K线(断点续跑: 只处理当日未入库股票)"""
    log.info("[JOB] 分钟线增量 - 触发")
    m_script = PROJECT_ROOT / "data" / "sjhq" / "分钟数据-国金QMT入库.py"
    today = datetime.now().strftime("%Y%m%d")
    ret_m = subprocess.run(
        [sys.executable, "-X", "utf8", str(m_script), "--start", today, "--end", today],
        cwd=str(PROJECT_ROOT),
    )
    log.info("[JOB] 分钟线增量 - 完成 (returncode=%s)", ret_m.returncode)


def job_pool_cur_pct_backfill():
    """13:00 / 15:00: 选股池「当前涨幅」(cur_pct) 定时更新 -- 用实时行情拉当日涨幅回填.

    13:00 取盘中实时涨幅; 15:00 收盘后 lastPrice 定格为当日收盘价, 即当日全天涨幅。
    非更新时刻页面显示为空(见 pool.html / pool_page.py 的时间判断)。
    """
    log.info("[JOB] 选股池当前涨幅定时更新 - 触发")
    try:
        from live_trading.live_loop import MarketDataProvider
        from lib.selection_store import (
            update_selection_cur_pct,
            latest_selection_day,
            latest_pool_trade_date,
        )
        # 仅更新最近一批入选股(当天选股)而非全库历史
        sel_day = latest_selection_day()
        pool_td = latest_pool_trade_date()
        day = sel_day or pool_td
        if not day:
            log.info("[JOB] 选股池为空(无入选记录), 跳过")
            return
        import pymysql
        from lib.backtest_data import _db_config
        cfg = _db_config()
        conn = pymysql.connect(**cfg)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT stock_code FROM trade_selection_pool "
                "WHERE stock_code <> '' AND trade_date = %s",
                (day,),
            )
            codes = [str(r[0]) for r in cur.fetchall()]
            cur.close()
        finally:
            conn.close()
        if not codes:
            log.info("[JOB] 当日无候选, 跳过")
            return
        mdp = MarketDataProvider()
        ticks = mdp.get_full_ticks(codes)
        pct_map = {}
        for code in codes:
            t = ticks.get(code)
            if not t:
                continue
            last = float(t.get("lastPrice") or 0)
            pre = 0.0
            for k in ("preClose", "lastClose", "last_close", "prevClose", "pre_close"):
                v = t.get(k)
                if v:
                    try:
                        pre = float(v)
                        break
                    except (TypeError, ValueError):
                        continue
            if last > 0 and pre > 0:
                pct_map[code] = round((last - pre) / pre * 100, 2)
        updated = update_selection_cur_pct(pct_map, trade_date=day)
        log.info("[JOB] 选股池当前涨幅定时更新 - 完成 (候选 %s, 更新 %s 行, day=%s)", len(codes), updated, day)
    except Exception as e:
        log.error("[JOB] 选股池当前涨幅定时更新失败: %s", e)


def job_market_volume():
    """15:02: 每日沪深两市成交总额入库 -- 新浪指数接口(上证指数+深证综指), 收盘后自动更新."""
    log.info("[JOB] 市场量能更新 - 触发")
    try:
        from lib.free_market import get_market_total_amount
        from lib.market_metrics import upsert_market_volume
        amounts = get_market_total_amount()
        today = datetime.now().strftime("%Y-%m-%d")
        upsert_market_volume(today, amounts, source="sina")
        log.info(
            "[JOB] 市场量能更新 - 完成 (date=%s, 沪=%s元, 深=%s元, 两市=%s元)",
            today,
            f"{amounts['sh']:.2e}",
            f"{amounts['sz']:.2e}",
            f"{amounts['total']:.2e}",
        )
    except Exception as e:
        log.error("[JOB] 市场量能更新失败: %s", e)


def job_start_engine():
    log.info("[JOB] 启动主循环 - 触发")
    sim = LiveSimRunner()
    if sim.status().get("running"):
        log.info("[JOB] 主循环已在运行, 跳过")
        return
    watch = merge_watch_codes([])
    if not watch:
        log.warning("[JOB] 监控池为空, 不启动")
        return
    # 恒为实盘 (不再有模拟盘): 主循环只同步持仓/盈亏/心跳, 不买卖
    # (买入 = 09:00 开盘买入窗口任务, 卖出 = 持仓页手动卖出)
    msg = sim.start(watch_stocks=watch, dry_run=False, init_positions=False, cycle_seconds=60)
    log.info(f"[JOB] 启动主循环 - 完成: {msg.splitlines()[0] if msg else 'OK'} (mode=REAL, 仅数据同步)")


def job_open_buy_window_start():
    """09:30 启动弱转强开盘买入窗口 (09:30:00-09:45:00 每 30 秒 tick 监控买入, 到点自动停止)"""
    log.info("[JOB] 开盘买入窗口 - 触发")
    try:
        from live_trading.open_buy_window import OpenBuyWindowRunner
        runner = OpenBuyWindowRunner()
        if runner.status().get("running"):
            log.info("[JOB] 开盘买入窗口任务已在运行, 跳过")
            return
        msg = runner.start()
        log.info(f"[JOB] 开盘买入窗口 - 完成: {msg.splitlines()[0] if msg else 'OK'}")
    except Exception as e:
        log.error("[JOB] 开盘买入窗口启动失败: %s", e)


def job_stop_engine():
    log.info("[JOB] 停止主循环 - 触发")
    sim = LiveSimRunner()
    if not sim.status().get("running"):
        log.info("[JOB] 主循环未在运行, 跳过")
        return
    msg = sim.stop()
    log.info("[JOB] 停止主循环 - 完成: %s", msg or "OK")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="实盘工作台调度器（与 app.py 共用 .env）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="模拟模式: 立刻把每个 job 跑一次然后退出",
    )
    parser.add_argument(
        "--job",
        choices=["data", "engine", "all"],
        default="all",
        help="只注册某一组 job: data | engine | all（默认）",
    )
    args = parser.parse_args()

    if args.simulate:
        log.info("=" * 60)
        log.info("[SIMULATE] 模拟模式")
        log.info("=" * 60)
        if args.job in ("data", "all"):
            job_data_refresh()
            job_daily_increment()
            job_minute_increment()
            job_pool_cur_pct_backfill()
            job_market_volume()
        if args.job in ("engine", "all"):
            job_start_engine()
            job_stop_engine()
        return

    sched = BlockingScheduler(timezone="Asia/Shanghai")

    if args.job in ("data", "all"):
        sched.add_job(
            job_data_refresh,
            id="data_refresh",
            name="08:30 数据增量",
            trigger=CronTrigger(hour=8, minute=30, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 08:30 CASE_A_BOARD_DATA_PREP_DIR -> run_daily.py")
        sched.add_job(
            job_daily_increment,
            id="daily_increment",
            name="15:00 日线增量",
            trigger=CronTrigger(hour=15, minute=0, timezone="Asia/Shanghai"),
        )
        log.info("[REG] 15:00 日线增量 (每日, 大QMT拉最近已收盘交易日 + 新浪修占位/价格异常)")
        sched.add_job(
            job_minute_increment,
            id="minute_increment",
            name="15:35 分钟线增量",
            trigger=CronTrigger(hour=15, minute=35, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 15:35 分钟线增量 (周一至周五, 当日1分钟K线)")
        sched.add_job(
            job_pool_cur_pct_backfill,
            id="pool_cur_pct_at13",
            name="13:00 选股池当前涨幅(盘中)",
            trigger=CronTrigger(hour=13, minute=0, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        sched.add_job(
            job_pool_cur_pct_backfill,
            id="pool_cur_pct_at15",
            name="15:00 选股池当前涨幅(收盘)",
            trigger=CronTrigger(hour=15, minute=0, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 13:00 / 15:00 选股池当前涨幅定时更新 (实时行情)")
        sched.add_job(
            job_market_volume,
            id="market_volume",
            name="15:02 市场量能",
            trigger=CronTrigger(hour=15, minute=2, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 15:02 市场量能 (每日两市成交总额, 新浪指数)")

    if args.job in ("engine", "all"):
        sched.add_job(
            job_start_engine,
            id="start_engine",
            name="09:00 启动主循环",
            trigger=CronTrigger(hour=9, minute=0, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        sched.add_job(
            job_open_buy_window_start,
            id="open_buy_window",
            name="09:30 开盘买入窗口",
            trigger=CronTrigger(hour=9, minute=30, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        sched.add_job(
            job_stop_engine,
            id="stop_engine",
            name="14:55 停止主循环",
            trigger=CronTrigger(hour=14, minute=55, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 09:00 / 14:55 引擎启停")
        log.info("[REG] 09:30 开盘买入窗口 (09:30:00-09:45:00 每 30s 判定买入)")

    log.info("=" * 60)
    log.info("[BOOT] 调度器前台运行（Ctrl+C 退出） cwd=%s", PROJECT_ROOT)
    log.info("       当前时间 %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("[EXIT] 调度器已退出")


if __name__ == "__main__":
    main()
