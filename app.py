# -*- coding: utf-8 -*-
# 25-AI量化系统 主入口 -- FastAPI + 挂载 Gradio (投研对话)
"""
单进程统一服务:
    FastAPI       -- 主框架 + REST + SSE
    Tailwind CSS  -- 前端 (CDN)
    Alpine.js     -- 前端交互 (CDN)
    Plotly.js     -- 图表 (CDN)
    Gradio        -- 仅用于投研对话, 挂载到 /chat (复用 pages/tab1_chat.py)

URL 结构:
    /            -- 默认重定向到 /live
    /chat/*      -- Gradio 投研对话
    /morning     -- 晨会分析 HTML (读库)
    /live        -- 实盘监控 HTML
    /backtest    -- 回测 HTML
    /review      -- 复盘归因 HTML (Brinson + Walk-Forward + 生命周期)
    /system      -- 系统状态 HTML
    /api/*       -- REST API
    /static/*    -- 静态资源 (CSS/JS)

启动:
    python app.py        -- 默认 7865 端口
"""

import os
import socket
import sys
from datetime import datetime
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 加载唯一 .env（路径见 lib.paths.ENV_FILE）
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
from dotenv import load_dotenv
from lib.paths import ENV_FILE, setup_sys_path
load_dotenv(ENV_FILE)
setup_sys_path()

import gradio as gr
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse

from routes import morning, live, review, system as sys_route, backtest, qmt_config, strategy_page, data_page, pool_page


# ============================================================
# Gradio 投研对话 -- 仅 Tab 1, 不带原来的多 Tab 外壳
# ============================================================

def build_chat_only_gradio():
    """只挂 Charles 投研对话, 不要顶部导航 (导航交给 FastAPI)"""
    from pages import tab1_chat
    with gr.Blocks(
        title="投研对话",
        theme=gr.themes.Soft(
            primary_hue="indigo",
            font=[gr.themes.GoogleFont("Inter"), "Microsoft YaHei", "sans-serif"],
        ),
        analytics_enabled=False,
        css="""
.gradio-container { max-width: 100% !important; padding: 16px !important; }
""",
    ) as app:
        tab1_chat.build_tab()
    return app


# ============================================================
# FastAPI 主应用
# ============================================================

api = FastAPI(title="AI 量化系统", docs_url="/api/docs", redoc_url=None)

# 静态资源
api.mount("/static", StaticFiles(directory=str(THIS_DIR / "static")), name="static")

# Jinja2 模板
templates = Jinja2Templates(directory=str(THIS_DIR / "templates"))

# REST 路由
api.include_router(morning.router,   prefix="/api/morning",  tags=["morning"])
api.include_router(live.router,      prefix="/api/live",     tags=["live"])
api.include_router(review.router,    prefix="/api/review",   tags=["review"])
api.include_router(sys_route.router, prefix="/api/system",   tags=["system"])
api.include_router(backtest.router,  prefix="/api/backtest", tags=["backtest"])
api.include_router(qmt_config.router, prefix="/api/qmt",     tags=["qmt"])
api.include_router(strategy_page.router, prefix="/api/strategy", tags=["strategy"])
api.include_router(data_page.router, prefix="/api/data", tags=["data"])
api.include_router(pool_page.router, prefix="/api/pool", tags=["pool"])


# ------------- 页面路由 -------------

@api.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/live")


@api.get("/chat", response_class=HTMLResponse)
def page_chat(request: Request):
    return templates.TemplateResponse("chat.html",
                                      {"request": request, "active": "chat"})


@api.get("/morning", response_class=HTMLResponse)
def page_morning(request: Request):
    return templates.TemplateResponse("morning.html",
                                      {"request": request, "active": "morning"})


@api.get("/live", response_class=HTMLResponse)
def page_live(request: Request):
    # 实盘视图 (只保留一套集成面板): 实时持仓 / 买入记录 / 监控池
    return templates.TemplateResponse("live.html",
                                      {"request": request, "active": "live",
                                       "view_mode": "real"})


@api.get("/backtest", response_class=HTMLResponse)
def page_backtest(request: Request):
    return templates.TemplateResponse("backtest.html",
                                      {"request": request, "active": "backtest"})


@api.get("/review", response_class=HTMLResponse)
def page_review(request: Request):
    return templates.TemplateResponse("review.html",
                                      {"request": request, "active": "review"})


@api.get("/system", response_class=HTMLResponse)
def page_system(request: Request):
    return templates.TemplateResponse("system.html",
                                      {"request": request, "active": "system"})


@api.get("/qmt", response_class=HTMLResponse)
def page_qmt(request: Request):
    return templates.TemplateResponse("qmt.html",
                                      {"request": request, "active": "qmt"})


@api.get("/strategy", response_class=HTMLResponse)
def page_strategy(request: Request):
    return templates.TemplateResponse("strategy.html",
                                      {"request": request, "active": "strategy"})


@api.get("/data", response_class=HTMLResponse)
def page_data(request: Request):
    return templates.TemplateResponse("data.html",
                                      {"request": request, "active": "data"})


@api.get("/pool", response_class=HTMLResponse)
def page_pool(request: Request):
    return templates.TemplateResponse("pool.html",
                                      {"request": request, "active": "pool"})


# ------------- 挂载 Gradio 到 /gradio-chat/ (供 /chat 页面 iframe 嵌入) -------------

gradio_app = build_chat_only_gradio()
api = gr.mount_gradio_app(api, gradio_app, path="/gradio-chat")


# ------------- 策略定时选股 (各策略按自身缺省执行时间, 周一至周五) -------------

_SELECTION_SCHEDULER = None  # 持有 APScheduler 引用, 防止被 GC


def _start_selection_scheduler():
    """APScheduler 后台调度: 每分钟检查, 对'已启用且配置了时针刻度'的策略在其各自选股时刻
    运行一次选股 (仅跑该策略, 结果写库+live_state). 未配置 schedule 的策略仅手动触发."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from lib.strategy_runner import (
            load_selection_config, run_one_selection, parse_schedule,
        )
        from lib.selection_store import ensure_tables
        ensure_tables()  # 首次运行建好每策略选股池/持仓表 (幂等)
    except Exception as e:
        print(f"[strategy] 定时选股调度器启动失败 (不影响 web): {e}", flush=True)
        return None

    def _job():
        now = datetime.now().strftime("%H:%M")
        try:
            cfg = load_selection_config()
        except Exception:
            return
        for inst in cfg.get("strategies", []):
            if not inst.get("enabled"):
                continue
            name = inst.get("name")
            if not name:
                continue
            times = parse_schedule(inst.get("schedule"))
            if not times:
                times = ["09:26"]  # 缺省执行时间 09:26:00 (未显式配置时)
            if now not in times:
                continue
            try:
                r = run_one_selection(name, trigger="cron")
                print(f"[strategy] 定时选股 {name} @{now}: {r.get('message', r.get('ok'))}", flush=True)
            except Exception as e:
                print(f"[strategy] 定时选股 {name} 异常: {type(e).__name__}: {e}", flush=True)

    # 昨日全市场量能阈值 (元): 高于此值才把当日选股池候选推入监控池
    # 实际值在 _push_watchpool_job 运行时动态读取界面配置 (config/pool_config.yaml), 缺省 2 万亿
    WATCH_POOL_MIN_MARKET_AMOUNT = 2.0e12  # 默认 2 万亿

    def _push_watchpool_job():
        """09:28 定时: 按昨日市场量能决定把哪批选股池候选推入监控池.

        选股完成后自动入监控池已取消, 统一由本任务驱动:
          - 昨日量能 > 阈值  → 推入「当日」选股池候选 (今天能开盘买);
          - 昨日量能 <= 阈值 → 推入「昨日」选股池候选, 且仅限「昨日收盘价 < 昨日开盘价」(阴线/低走)
            且「昨日涨幅 > -5%」(跌幅不超过5%) 的股票 —— 量能不足时退而求其次买昨日回调但未大跌的候选。
        """
        import pymysql
        # 运行时读取界面配置的昨日量能阈值 (万亿 -> 元), 缺省 2 万亿
        try:
            from lib.pool_config import load_min_market_amount_yuan, load_min_market_yi
            _threshold = load_min_market_amount_yuan()
            _yi = load_min_market_yi()
        except Exception:
            _threshold = WATCH_POOL_MIN_MARKET_AMOUNT
            _yi = 2.0
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            from lib.selection_store import (
                _db_config, query_selection_pool, sync_pool_to_watch_pool,
            )
            cfg = _db_config()
            conn = pymysql.connect(connect_timeout=5, read_timeout=15, **cfg)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT trade_date, total_amount FROM trade_market_volume "
                        "WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1",
                        (today,),
                    )
                    row = cur.fetchone()
                prev_date = str(row[0]) if row else None
                amount = float(row[1]) if row and row[1] is not None else None
                # 昨日阴线集合: 昨日收盘价 < 昨日开盘价 (阴线), 且昨日涨幅 > 下限(防跌太狠) (从日线表取)
                yin_codes = None
                if prev_date:
                    # 昨日涨幅 = 昨日收盘 / 前一日收盘 - 1; 需 join 前一日收盘价 (trade_date < 昨日的最大交易日)
                    # 下限 (昨日阴线候选的涨幅下限 %) 从选股池界面配置读取
                    from lib.pool_config import load_prev_drop_min_pct
                    prev_drop_min = load_prev_drop_min_pct() / 100.0
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "SELECT DISTINCT a.stock_code FROM trade_stock_daily a "
                            "JOIN trade_stock_daily b "
                            "  ON b.stock_code = a.stock_code "
                            " AND b.trade_date = (SELECT MAX(t2.trade_date) FROM trade_stock_daily t2 "
                            "                     WHERE t2.stock_code = a.stock_code "
                            "                       AND t2.trade_date < %s) "
                            "WHERE a.trade_date = %s "
                            "  AND a.close_price < a.open_price "
                            "  AND b.close_price > 0 "
                            "  AND (a.close_price / b.close_price - 1.0) > %s",
                            (prev_date, prev_date, prev_drop_min),
                        )
                        yin_codes = {str(r[0]) for r in cur2.fetchall()}
            finally:
                conn.close()

            # 分支一: 量能充足 (>阈值万亿) → 推当日选股候选
            if amount is not None and amount > _threshold:
                rows = query_selection_pool() or []
                today_rows = [r for r in rows
                              if (str(r.get("selected_at") or "")[:10] == today
                                  or str(r.get("trade_date") or "")[:10] == today)]
                codes = [str(r["stock_code"]) for r in today_rows]
                if not codes:
                    print("[watch_pool] @09:28 当日选股池无候选, 不推送", flush=True)
                    return
                added = sync_pool_to_watch_pool(codes)
                print(f"[watch_pool] @09:28 量能充足({amount}元, 阈值{_yi}万亿), 推入当日候选 "
                      f"{len(added)} 只; 共 {len(codes)} 只", flush=True)
                return

            # 分支二: 量能不足 (<=阈值万亿) → 推昨日选股候选, 且需昨日收盘<开盘
            if not prev_date:
                print("[watch_pool] @09:28 无昨日量能数据, 不推送", flush=True)
                return
            prev_rows = query_selection_pool(trade_date=prev_date) or []
            codes = [str(r["stock_code"]) for r in prev_rows
                     if str(r["stock_code"]) in (yin_codes or set())]
            if not codes:
                print(f"[watch_pool] @09:28 量能不足({amount}元, 阈值{_yi}万亿), "
                      f"昨日({prev_date})候选 {len(prev_rows)} 只, 无满足昨日收<开者, 不推送",
                      flush=True)
                return
            added = sync_pool_to_watch_pool(codes)
            print(f"[watch_pool] @09:28 量能不足({amount}元, 阈值{_yi}万亿), 推入昨日({prev_date})阴线候选 "
                  f"{len(added)} 只; 符合 {len(codes)} 只 / 昨日候选 {len(prev_rows)} 只",
                  flush=True)
        except Exception as e:
            print(f"[watch_pool] @09:28 推送异常: {type(e).__name__}: {e}", flush=True)

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _job,
        trigger=CronTrigger(minute="*", second=0, day_of_week="mon-fri",
                            timezone="Asia/Shanghai"),
        id="selection_per_minute",
        name="策略按各自选股时间触发",
    )
    sched.add_job(
        _push_watchpool_job,
        trigger=CronTrigger(hour=9, minute=28, second=0, day_of_week="mon-fri",
                            timezone="Asia/Shanghai"),
        id="push_selection_to_watch_pool",
        name="09:28 选股池候选按量能条件推入监控池",
    )
    sched.start()
    print("[strategy] 定时选股已启动: 每分钟检查, 各策略按自身 schedule 触发 (周一至周五)", flush=True)
    return sched


# ============================================================
# 启动
# ============================================================

def _find_free_port(start_port: int, host: str = "127.0.0.1", max_tries: int = 50) -> int:
    """从 start_port 起找空闲端口 (Windows 不能用 SO_REUSEADDR)"""
    for offset in range(max_tries):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"在 {start_port}-{start_port+max_tries-1} 找不到空闲端口")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI 量化交易系统 (FastAPI + Gradio)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("DASHBOARD_PORT", 7865)))
    # 默认 127.0.0.1: 仅本机访问, uvicorn 启动日志显示 127.0.0.1, 浏览器可直接点开;
    # 需要 LAN 上其他设备 (手机 / 同事电脑) 访问时, 启动加 `--host 0.0.0.0`.
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址, 默认 127.0.0.1 (仅本机); LAN 共享用 0.0.0.0")
    parser.add_argument("--no-auto-port", action="store_true")
    args = parser.parse_args()

    desired_port = args.port
    actual_port = desired_port
    if not args.no_auto_port:
        actual_port = _find_free_port(desired_port, host=args.host)
        if actual_port != desired_port:
            print(f"[INFO] 端口 {desired_port} 已被占用, 自动切换到 {actual_port}")

    # banner 上显示的地址: 0.0.0.0 在浏览器里点不开, 统一展示成 127.0.0.1 引导用户;
    # uvicorn 启动日志会按 args.host 真实显示 (0.0.0.0 / 127.0.0.1).
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host

    print()
    print("=" * 70)
    print("  AI 量化交易系统启动 (FastAPI + Tailwind + Alpine + Gradio)")
    print("=" * 70)
    print(f"  Web UI:    http://{display_host}:{actual_port}  (默认进入 /live)")
    print(f"  API docs:  http://{display_host}:{actual_port}/api/docs")
    print(f"  Gradio:    http://{display_host}:{actual_port}/gradio-chat/  (内嵌于 /chat)")
    if args.host in ("0.0.0.0", "::"):
        print(f"  LAN 共享:  已绑定所有网卡, 局域网内可用本机 IP 访问")
    print(f"  默认 dry-run, 不会真下单")
    print(f"  定时选股:  每分钟检查, 各策略按自身配置的选股时间触发 (周一至周五)")
    print("=" * 70)
    print()

    # 注册策略定时选股 (后台线程, 保存引用防止 GC)
    global _SELECTION_SCHEDULER
    _SELECTION_SCHEDULER = _start_selection_scheduler()

    uvicorn.run(api, host=args.host, port=actual_port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
