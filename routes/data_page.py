# -*- coding: utf-8 -*-
# 数据获取路由 -- REST
"""
GET  /api/data/scripts  -- 返回所有数据获取/准备脚本列表
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from lib.paths import PROJECT_ROOT, ENV_FILE
from lib.ingest_progress import load as load_progress

router = APIRouter()


def _case_a_prep_dir() -> str:
    raw = (os.environ.get("CASE_A_BOARD_DATA_PREP_DIR") or "").strip()
    return raw if raw else ""


def _env_has(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


# 已有手写条目的脚本名 (不再出现在"独立下载脚本"动态扫描中)
_MANUAL_ITEMS = {
    "日线数据-国金QMT入库.py",
    "分钟数据-国金QMT入库.py",
    "fix_当日日线_逐步.py",
    "fix_新浪9月7日.py",
}

# 动态扫描 data/sjhq 目录, 生成"独立下载脚本"列表 (后台新增脚本自动显示)
def _scan_sjhq_scripts() -> list:
    sjhq = PROJECT_ROOT / "data" / "sjhq"
    out = []
    if not sjhq.exists():
        return out
    for p in sorted(sjhq.glob("*.py")):
        if p.name.startswith("__") or p.name in _MANUAL_ITEMS:
            continue
        name = p.name
        if "akshare" in name:
            source = "AkShare (东方财富/新浪, 免 Token)"
            detail = "下载" + ("日线" if "日线" in name else "分钟" if "分钟" in name else "财务") + "数据, 保存 CSV"
        elif "tushare" in name:
            source = "Tushare Pro (需 TUSHARE_TOKEN)"
            detail = "下载" + ("日线" if "日线" in name else "分钟" if "分钟" in name else "财务") + "数据, 保存 CSV"
        else:
            source = "QMT (xtquant/xtdata)"
            detail = "下载" + ("日线" if "日线" in name else "分钟" if "分钟" in name else "财务") + "数据, 保存 CSV"
        out.append({
            "name": name,
            "label": name.replace(".py", ""),
            "description": f"{detail}。数据源: {source}",
            "schedule": "手动运行",
            "path": str(p),
            "status": "内置",
            "group": "独立下载脚本",
            "io": "读取行情源 → 写 CSV",
        })
    return out


@router.get("/scripts")
def data_scripts():
    """返回所有数据获取/准备脚本"""
    prep_dir = _case_a_prep_dir()
    run_daily = (Path(prep_dir) / "run_daily.py") if prep_dir else None
    run_daily_exists = run_daily and run_daily.exists()

    scripts = [
        # ===== 数据写入 =====
        {
            "name": "run_daily.py (外部)",
            "label": "板块数据增量 (外部项目, 可选)",
            "description": "从 QMT/xtdata 拉取昨日 K 线 + 板块指数，INSERT 写入 MySQL trade_stock_daily 表。"
                           "由 scheduler.py 08:30 定时触发；需配置 CASE_A_BOARD_DATA_PREP_DIR 指向外部项目目录，"
                           "未配置时自动跳过（当前主力为 15:35 的 日线数据-国金QMT入库.py）",
            "schedule": "08:30 周一至周五 (由 scheduler.py 触发)",
            "path": str(run_daily) if run_daily else "(未配置 CASE_A_BOARD_DATA_PREP_DIR)",
            "status": "已配置" if run_daily_exists else "未配置",
            "group": "数据写入",
            "io": "写入 MySQL trade_stock_daily",
        },
        {
            "name": "日线数据-国金QMT入库.py",
            "label": "日线K线入库 (国金大QMT)",
            "description": "通过 bigqmt ZMQ 桥(端口15615)从国金大QMT 拉取全A股日K，幂等写入 MySQL "
                           "trade_stock_daily (唯一键 stock_code+trade_date)。"
                           "scheduler.py 每日 20:00 以 --increment --date=最近已收盘交易日 增量模式触发；"
                           "也可手动 --start/--end/--only 回补历史区间",
            "schedule": "20:00 每日 (由 scheduler.py 触发)",
            "path": str(PROJECT_ROOT / "data" / "sjhq" / "日线数据-国金QMT入库.py"),
            "status": "内置",
            "group": "数据写入",
            "io": "读取 BigQMT(15615) → 写入 MySQL trade_stock_daily",
        },
        {
            "name": "分钟数据-国金QMT入库.py",
            "label": "分钟K线入库 (国金大QMT)",
            "description": "通过 bigqmt ZMQ 桥(端口15615)从国金大QMT 拉取全A股1分钟K线，幂等写入 MySQL "
                           "trade_stock_minute (唯一键 stock_code+trade_time)。"
                           "scheduler.py 15:35 以 --start/--end=当日 增量模式定时触发(断点续跑)；"
                           "也可手动 --start/--end 指定范围回补历史",
            "schedule": "15:35 周一至周五 (由 scheduler.py 增量触发)",
            "path": str(PROJECT_ROOT / "data" / "sjhq" / "分钟数据-国金QMT入库.py"),
            "status": "内置",
            "group": "数据写入",
            "io": "读取 BigQMT(15615) → 写入 MySQL trade_stock_minute",
        },

        # ===== 实时行情 =====
        {
            "name": "live_trading/live_loop.py",
            "label": "实盘行情拉取 (xtdata)",
            "description": "MarketDataProvider 类，从 xtdata 拉取实时 tick + 历史 K 线。"
                           "get_full_tick() 实时报价，download_history_data() + get_market_data_ex() 获取 K 线",
            "schedule": "每分钟 (主循环周期)",
            "path": str(PROJECT_ROOT / "live_trading" / "live_loop.py"),
            "status": "内置",
            "group": "实时行情",
            "io": "读取 xtdata (QMT)",
        },
        {
            "name": "lib/live_simulator.py",
            "label": "模拟盘行情",
            "description": "模拟盘引擎，拉取最新 close 价格用于模拟下单与盈亏计算",
            "schedule": "每分钟 (模拟盘周期)",
            "path": str(PROJECT_ROOT / "lib" / "live_simulator.py"),
            "status": "内置",
            "group": "实时行情",
            "io": "读取 MySQL/xtdata",
        },

        # ===== 历史数据 =====
        {
            "name": "lib/backtest_data.py",
            "label": "历史 K 线加载层",
            "description": "统一数据源优先级: MySQL trade_stock_daily (优先) -> xtdata download_history_data (fallback)。"
                           "load_daily_kline() 返回日 K DataFrame，供回测/因子计算/龙头策略共用",
            "schedule": "按需调用 (回测/策略评估/因子计算)",
            "path": str(PROJECT_ROOT / "lib" / "backtest_data.py"),
            "status": "内置",
            "group": "历史数据",
            "io": "读取 MySQL trade_stock_daily / xtdata",
        },

        # ===== 调度 =====
        {
            "name": "scheduler.py",
            "label": "定时调度器",
            "description": "APScheduler + cron，按 A 股交易时段自动启停 + 触发数据增量",
            "schedule": "20:00 日线增量(每日) / 08:30 外部数据增量 / 09:30 启动主循环 / 14:55 停止主循环 / 15:35 分钟线增量",
            "path": str(PROJECT_ROOT / "scheduler.py"),
            "status": "内置",
            "group": "调度",
            "io": "触发 run_daily.py / 日线/分钟数据-国金QMT入库.py / 启停引擎",
        },

        # ===== 数据修复 =====
        {
            "name": "fix_当日日线_逐步.py",
            "label": "当日日线修复 (BigQMT 覆盖)",
            "description": "一次性修复脚本: 用 BigQMT 覆盖 trade_stock_daily 指定交易日的真实行情,"
                           " 尤其修复占位行。--date 指定交易日, 可选 --only/--limit 限定范围",
            "schedule": "手动运行 (发现占位行/异常时)",
            "path": str(PROJECT_ROOT / "data" / "sjhq" / "fix_当日日线_逐步.py"),
            "status": "内置",
            "group": "数据修复",
            "io": "读取 BigQMT(15615) → 覆盖 MySQL trade_stock_daily",
        },
        {
            "name": "fix_新浪9月7日.py",
            "label": "新浪占位行修复",
            "description": "用新浪直连(免费/稳定) 拉取真实行情, 覆盖 trade_stock_daily 目标交易日的占位行"
                           "(volume 新浪为股 -> 存库转手 ÷100), 不破坏已有真实数据。--date 指定交易日",
            "schedule": "手动运行 (发现占位行/价格异常时)",
            "path": str(PROJECT_ROOT / "data" / "sjhq" / "fix_新浪9月7日.py"),
            "status": "内置",
            "group": "数据修复",
            "io": "读取新浪 → 覆盖 MySQL trade_stock_daily",
        },

        # ===== 晨会分析 =====
        {
            "name": "morning_brief/graph.py",
            "label": "晨会分析工作流",
            "description": "从 MySQL wucai_trade 拉数据 -> 行业排名 -> 因子计算 -> 候选股票池 -> HTML 报告",
            "schedule": "按需手动触发",
            "path": str(PROJECT_ROOT / "morning_brief" / "graph.py"),
            "status": "内置",
            "group": "晨会分析",
            "io": "读取 MySQL wucai_trade",
        },
        {
            "name": "morning_brief/lib/factor_runner.py",
            "label": "因子批量计算",
            "description": "遍历股票代码，从 trade_stock_daily 加载 K 线并计算多因子 (MOM/RSI/BIAS 等)",
            "schedule": "晨会分析时调用",
            "path": str(PROJECT_ROOT / "morning_brief" / "lib" / "factor_runner.py"),
            "status": "内置",
            "group": "晨会分析",
            "io": "读取 MySQL trade_stock_daily",
        },
        {
            "name": "morning_brief/lib/rotation_runner.py",
            "label": "行业轮动计算",
            "description": "计算行业板块轮动指标，生成行业排名数据",
            "schedule": "晨会分析时调用",
            "path": str(PROJECT_ROOT / "morning_brief" / "lib" / "rotation_runner.py"),
            "status": "内置",
            "group": "晨会分析",
            "io": "读取 MySQL",
        },
        {
            "name": "morning_brief/pusher.py",
            "label": "晨报推送",
            "description": "将生成的晨会分析 HTML 推送到指定渠道",
            "schedule": "晨会分析完成后触发",
            "path": str(PROJECT_ROOT / "morning_brief" / "pusher.py"),
            "status": "内置",
            "group": "晨会分析",
            "io": "输出 HTML 推送",
        },

        # ===== 策略数据 =====
        {
            "name": "dragon_strategy/dragon_backtest.py",
            "label": "龙头策略回测数据",
            "description": "从 trade_stock_daily 读取区间内所有交易日，执行龙头战法回测",
            "schedule": "按需调用 (回测)",
            "path": str(PROJECT_ROOT / "dragon_strategy" / "dragon_backtest.py"),
            "status": "内置",
            "group": "策略数据",
            "io": "读取 MySQL trade_stock_daily",
        },
        {
            "name": "ml_strategy/feature_engine.py",
            "label": "ML 特征工程",
            "description": "从 K 线数据构造机器学习特征 (技术指标/量价因子)，供 ML 概率因子模型使用",
            "schedule": "按需调用 (ML 训练/预测)",
            "path": str(PROJECT_ROOT / "ml_strategy" / "feature_engine.py"),
            "status": "内置",
            "group": "策略数据",
            "io": "读取 K 线数据",
        },
        {
            "name": "ml_strategy/ml_prob_runner.py",
            "label": "ML 概率因子模型",
            "description": "XGBoost 概率因子选股，基于特征工程输出进行训练与预测",
            "schedule": "按需调用 (ML 训练/预测)",
            "path": str(PROJECT_ROOT / "ml_strategy" / "ml_prob_runner.py"),
            "status": "内置",
            "group": "策略数据",
            "io": "读取特征数据",
        },

        # ===== 基础设施 =====
        {
            "name": "morning_brief/lib/db_config.py",
            "label": "数据库连接配置",
            "description": "从 .env 读取 WUCAI_SQL_* 配置，提供 get_connection / execute_query",
            "schedule": "被其他模块引用",
            "path": str(PROJECT_ROOT / "morning_brief" / "lib" / "db_config.py"),
            "status": "内置",
            "group": "基础设施",
            "io": "MySQL 连接",
        },

        # ===== 独立下载脚本 (data/sjhq 动态扫描) =====
        # 三类数据源: QMT / AkShare / Tushare，各覆盖 日线 / 分钟 / 财务
        # 由 _scan_sjhq_scripts() 动态生成, 后台新增脚本自动出现在列表
    ]
    scripts.extend(_scan_sjhq_scripts())

    # 环境配置状态
    env_status = {
        "CASE_A_BOARD_DATA_PREP_DIR": _case_a_prep_dir() or "(未配置)",
        "WUCAI_SQL_HOST": os.environ.get("WUCAI_SQL_HOST", "") or "未配置",
        "WUCAI_SQL_PORT": os.environ.get("WUCAI_SQL_PORT", ""),
        "WUCAI_SQL_DB": os.environ.get("WUCAI_SQL_DB", ""),
        "QMT_PATH": os.environ.get("QMT_PATH", "") or "未配置",
        "TUSHARE_TOKEN": "已配置" if _env_has("TUSHARE_TOKEN") else "未配置",
    }

    # 合并数据入库脚本运行进度 (outputs/ingest_progress.json)
    # 完成/失败状态保留 5 分钟后过期, 避免界面常驻陈旧信息
    progress = load_progress()
    if progress:
        try:
            upd = datetime.strptime(progress.get("updated_at", ""), "%Y-%m-%d %H:%M:%S")
            if progress.get("status") in ("done", "error") and time.time() - upd.timestamp() > 300:
                progress = {}
        except Exception:
            pass
    if progress:
        for s in scripts:
            if s["name"] == progress.get("script"):
                s["progress"] = dict(progress)
                break

    return {"scripts": scripts, "env": env_status}
