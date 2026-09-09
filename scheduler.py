# -*- coding: utf-8 -*-
# CASE-AI 量化系统: 定时调度器（实盘时段启停 + 可选数据增量）
"""
TradingScheduler -- 按 A 股交易时段自动启停 +（可选）板块日更脚本

单独进程的原因:
    - Web (app.py) 与调度解耦：浏览器关了不影响调度，调度挂了不影响 Web。
    - APScheduler + cron。

5 个 cron job（时区 Asia/Shanghai）:
    20:00   job_daily_increment   -> 日线增量入库 (大QMT拉最近已收盘交易日 + 新浪修占位/价格异常), 每日执行
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
    """20:00: 每日日线增量入库 -- 大QMT拉取最近已收盘交易日未入库日线, 占位/价格异常用新浪修复"""
    log.info("[JOB] 日线增量 - 触发")
    script = PROJECT_ROOT / "data" / "sjhq" / "日线数据-国金QMT入库.py"
    target = _latest_trade_date()
    ret = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), "--increment", "--date", target],
        cwd=str(PROJECT_ROOT),
    )
    log.info("[JOB] 日线增量 - 完成 (returncode=%s, target=%s)", ret.returncode, target)


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
    # 恒为实盘 (不再有模拟盘): 直接以 dry_run=False 启动, 满足买入规则即真实下单
    msg = sim.start(watch_stocks=watch, dry_run=False, init_positions=False, cycle_seconds=60)
    log.info(f"[JOB] 启动主循环 - 完成: {msg.splitlines()[0] if msg else 'OK'} (mode=REAL)")


def job_open_buy_window_start():
    """09:29: 启动弱转强开盘买入窗口 (09:29-09:35 每 15 秒 tick 监控买入, 到点自动停止)"""
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
            name="20:00 日线增量",
            trigger=CronTrigger(hour=20, minute=0, timezone="Asia/Shanghai"),
        )
        log.info("[REG] 20:00 日线增量 (每日, 大QMT拉最近已收盘交易日 + 新浪修占位/价格异常)")
        sched.add_job(
            job_minute_increment,
            id="minute_increment",
            name="15:35 分钟线增量",
            trigger=CronTrigger(hour=15, minute=35, day_of_week="mon-fri",
                                timezone="Asia/Shanghai"),
        )
        log.info("[REG] 15:35 分钟线增量 (周一至周五, 当日1分钟K线)")

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
            name="09:29 弱转强开盘买入窗口",
            trigger=CronTrigger(hour=9, minute=29, day_of_week="mon-fri",
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
        log.info("[REG] 09:29 弱转强开盘买入窗口 (09:29-09:35 每 15s 判定买入)")

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
