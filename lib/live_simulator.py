# -*- coding: utf-8 -*-
# 模拟盘 / 实盘 runner -- 后台线程跑 LiveTradingLoop
"""
设计:
    - 启动: 后台 daemon 线程跑 LiveTradingLoop.run_once() 循环 (默认每 60 秒)
    - 持仓: 从 config/mock_positions.yaml 读取
    - 模式: dry_run (默认, 模拟下单) / 实盘 (连真实 miniQMT, 慎用!)
    - 策略: 从 config/strategies.yaml 读路由表, 注入 StrategyRouter 到 loop
    - 行情: xtdata 真实数据

数据写到 outputs/live_state.json, dashboard 5 秒轮询自动刷新
"""

from __future__ import annotations
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional


CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "mock_positions.yaml"
STRATEGY_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "strategies.yaml"
WATCH_POOL_FILE = Path(__file__).resolve().parent.parent / "config" / "watch_pool.yaml"


# ============================================================
# 策略路由配置 -- 读 / 写 strategies.yaml
# ============================================================

def load_strategy_config() -> dict:
    """读 config/strategies.yaml -- 返回 {default, per_stock}"""
    if not STRATEGY_CONFIG_FILE.exists():
        return {"default": "macd_5min", "per_stock": {}}
    try:
        import yaml
        cfg = yaml.safe_load(STRATEGY_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return {
            "default":   cfg.get("default", "macd_5min"),
            "per_stock": cfg.get("per_stock", {}) or {},
        }
    except Exception as e:
        print(f"[WARN] 读 strategies.yaml 失败: {e}")
        return {"default": "macd_5min", "per_stock": {}}


def save_strategy_config(default: str, per_stock: dict) -> None:
    """写 config/strategies.yaml -- 前端 '应用策略配置' 调"""
    import yaml
    try:
        STRATEGY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 实盘监控 -- 策略路由表 (由 /live 页面写入)\n"
            "# 修改后, 在页面点 '应用策略配置' 即可热加载, 无需重启\n"
            "# MACD: macd_5min=5分钟K(日内) / macd_1d=日K(12/26/9); 另有 dual_ma_5min / ma20_hold / multi_factor_top / dragon_picker / grid_classic\n\n"
        )
        body = yaml.safe_dump(
            {"default": default, "per_stock": dict(per_stock or {})},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        STRATEGY_CONFIG_FILE.write_text(header + body, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入 {STRATEGY_CONFIG_FILE}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"写入 strategies.yaml 失败: {e}") from e


def _per_stock_to_selection_cfg(per_stock: dict) -> dict:
    """兼容旧调用: 把 {code->strategy} 折算成新模型 {universe, strategies:[{name, universe:code_list, code_list, enabled}]}."""
    groups: Dict[str, list] = {}
    for code, strat in (per_stock or {}).items():
        groups.setdefault(str(strat), []).append(str(code).strip())
    strategies = []
    for strat, codes in groups.items():
        strategies.append({
            "name": strat,
            "label": strat,
            "enabled": True,
            "universe": "code_list",
            "code_list": codes,
            "lookback_days": 60,
            "top_n": 0,
            "selection": {"mode": "code_list", "filters": {},
                          "sort_by": "amount", "sort_desc": True},
        })
    return {"universe": {"source": "mysql"}, "strategies": strategies}


# ============================================================
# 自选监控池 watch_pool.yaml -- 无持仓也会拉行情、算信号、可触发买入
# ============================================================

def load_watch_pool() -> dict:
    """读 config/watch_pool.yaml -- 返回 {codes: [str,...]}"""
    if not WATCH_POOL_FILE.exists():
        return {"codes": []}
    try:
        import yaml
        data = yaml.safe_load(WATCH_POOL_FILE.read_text(encoding="utf-8")) or {}
        codes = data.get("codes", []) or []
        return {"codes": [str(c).strip() for c in codes if str(c).strip()]}
    except Exception as e:
        print(f"[WARN] 读 watch_pool.yaml 失败: {e}")
        return {"codes": []}


def save_watch_pool(codes: List[str]) -> None:
    """写 config/watch_pool.yaml"""
    import yaml
    try:
        WATCH_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 监控代码列表 -- 与 mock 持仓、per_stock 合并为最终监控列表 (去重)\n"
            "# 也可通过页面「添加股票并绑定策略」写入\n\n"
        )
        body = yaml.safe_dump(
            {"codes": [c.strip() for c in (codes or []) if str(c).strip()]},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        WATCH_POOL_FILE.write_text(header + body, encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"无法写入 {WATCH_POOL_FILE}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"写入 watch_pool 失败: {e}") from e


def merge_watch_codes(ui_codes: Optional[List[str]] = None) -> List[str]:
    """
    合并监控代码 (去重, 顺序: 页面输入 -> 持仓 -> 自选池 -> 策略路由里的代码)

    ui_codes: 运行控制里「额外监控」逗号分隔解析后的列表, 可为空
    """
    ui_codes = ui_codes or []
    seen = set()
    out: List[str] = []

    def push(c: str) -> None:
        c = (c or "").strip()
        if not c or c in seen:
            return
        seen.add(c)
        out.append(c)

    for c in ui_codes:
        push(c)
    mock = load_mock_config()
    for p in mock.get("positions", []):
        push(str(p.get("code", "")))
    wp = load_watch_pool()
    for c in wp.get("codes", []):
        push(c)
    st = load_strategy_config()
    for c in (st.get("per_stock") or {}).keys():
        push(str(c).strip())
    # 新模型: 并入策略选股配置里 universe=code_list 的码表
    try:
        from lib.strategy_runner import load_selection_config
        for s in load_selection_config().get("strategies", []):
            if s.get("universe") == "code_list":
                for c in (s.get("code_list") or []):
                    push(str(c).strip())
    except Exception:
        pass
    # 并入当日选股池 (今日各策略自动候选): 当日监控中的股票优先纳入, 避免「监控池为空」
    # 只取 trigger='cron' (策略自动选股); 手动选股(trigger='manual')不入监控池
    try:
        from datetime import date
        from lib.selection_store import query_selection_pool
        for r in query_selection_pool(trade_date=date.today().isoformat(), trigger="cron"):
            push(str(r.get("stock_code", "")))
    except Exception:
        pass
    return out


# ============================================================
# A 股交易时段判断 -- 9:30-11:30 + 13:00-15:00, 仅工作日
# 用于跳过非盘中循环 (避免在收盘后/周末仍触发信号)
# ============================================================

def is_a_share_trading_hour(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    # 工作日 (周一到周五)
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as dt_time
    return ((dt_time(9, 30) <= t <= dt_time(11, 30))
            or (dt_time(13, 0) <= t <= dt_time(15, 0)))


def load_mock_config() -> dict:
    """读 config/mock_positions.yaml -- 学员可改这个文件改持仓"""
    if not CONFIG_FILE.exists():
        return {"capital": 1_000_000, "positions": []}
    try:
        import yaml
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[WARN] 读 mock_positions.yaml 失败: {e}")
        return {"capital": 1_000_000, "positions": []}


def _latest_close_map(codes: List[str]) -> dict:
    """批量拉每只票的「最新一根日 K close」, 给 build_positions 当 cur_price.

    背景: 模拟盘 worker 在非交易时段会跳过 (不拉 tick), 所以持仓 cur_price 会卡
    在初始值; 之前直接拿 cost 当初始价 -> 表里浮盈/亏永远 0, 与「4-1 持有至今」
    的真实涨跌脱节. 现在启动 / 重置持仓时, 用 MySQL/xtdata 日 K 的最新 close
    刷新一次, 即便不在盘中, 用户也能看到真实的持有期浮盈.

    返回: {code: latest_close}; 拿不到的 code 不出现 -> 调用方回退到 cost
    """
    if not codes:
        return {}
    try:
        from lib.backtest_data import load_daily_kline
    except Exception as e:
        print(f"[WARN] _latest_close_map: import backtest_data 失败 -> {e}", flush=True)
        return {}
    out = {}
    # 拉近 30 天日 K 取最新一根 close, 区间宽一点容错节假日 / 周末
    from datetime import date, timedelta
    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=45)).strftime("%Y-%m-%d")
    for code in codes:
        try:
            df = load_daily_kline(code, start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"[WARN] 拉 {code} 最新 close 失败: {e}", flush=True)
            continue
        if df is None or df.empty:
            continue
        out[code] = float(df.iloc[-1]["close"])
    return out


def build_positions_from_config() -> List[dict]:
    """从 config 构建标准化的 positions list (每只补齐当前价/市值/盈亏字段)

    cur_price 取自最新一根日 K close (容错: 拿不到回退到 cost), 这样:
      - 持仓表里的浮盈/亏 = (最新 close - cost) * volume, 反映真实持有期表现
      - calc_today_pnl 基于 position.pnl 求和 -> today_pnl 和持仓表对得上
      - 盘中真实 tick 来了, live_loop 会继续覆盖 cur_price (无副作用)
    """
    cfg = load_mock_config()
    raw_positions = cfg.get("positions", []) or []
    codes = [p.get("code", "") for p in raw_positions if p.get("code")]
    close_map = _latest_close_map(codes)

    positions = []
    for p in raw_positions:
        code = p.get("code", "")
        cost = float(p.get("cost", 0))
        volume = int(p.get("volume", 0))
        cur_price = float(close_map.get(code, cost))
        market_value = volume * cur_price
        pnl = (cur_price - cost) * volume
        pnl_pct = (cur_price / cost - 1) if cost > 0 else 0.0
        positions.append({
            "code":         code,
            "name":         p.get("name", ""),
            "volume":       volume,
            "cost":         cost,
            "cur_price":    round(cur_price, 4),
            "market_value": round(market_value, 2),
            "pnl":          round(pnl, 2),
            "pnl_pct":      round(pnl_pct, 4),
        })
    return positions


# ============================================================
# 历史订单回填 -- 模块级函数 (start() 自动调 + cli 也能单跑)
# ============================================================

def seed_historical_orders(force: bool = False) -> str:
    """从 SIM_HISTORY_START_DATE 至今, 按每只票路由策略跑一遍回测引擎,
    把 trades (已撮合成交) 转成 orders 写入 live_state.json.

    设计:
        - 不启动主循环, 不连券商, 纯历史日 K + 回测引擎跑出来的"假装下单"
        - 仅 state.orders 为空 (或 force=True) 才写, 避免重复
        - 每个 trade -> 1 个 order (含 ts/code/side/qty/price/amount), status="dry_run"
        - 调用方:
            1) LiveSimRunner.start() 启动时自动调 (与 signals/pnl 回放并列)
            2) cli 单跑: python lib/live_simulator.py --backfill-orders
        - 复用 run_backtest -- 撮合规则一致 (T+1, 整手, 手续费), 不另搭一套撮合

    Returns:
        日志字符串 (打印 + 给前端 status 看)
    """
    from lib.paths import OUTPUTS_LIVE_STATE
    import json

    if not OUTPUTS_LIVE_STATE.exists():
        return "[ERR] live_state.json 不存在, 先启动一次模拟盘 (启动会创建空 state)"

    s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    if s.get("orders") and not force:
        return f"[INFO] orders 已有 {len(s.get('orders'))} 条, 跳过回填 (--force 强制覆盖)"

    cfg = load_mock_config()
    positions = cfg.get("positions") or []
    if not positions:
        return "[ERR] mock_positions.yaml 没有持仓配置"

    strat_cfg = load_strategy_config()
    per_stock = strat_cfg.get("per_stock") or {}
    default_strat = strat_cfg.get("default", "macd_1d")

    start_date = os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")
    from datetime import date
    end_date = date.today().strftime("%Y-%m-%d")

    try:
        from lib.backtest_engine import run_backtest
    except Exception as e:
        return f"[ERR] import backtest_engine 失败: {e}"

    new_orders: List[dict] = []
    detail_lines: List[str] = []
    for p in positions:
        code = str(p.get("code", "")).strip()
        name = p.get("name", "")
        if not code:
            continue
        strat = per_stock.get(code) or default_strat
        try:
            r = run_backtest(stock_code=code, strategy_name=strat,
                             start_date=start_date, end_date=end_date)
        except Exception as e:
            detail_lines.append(f"  [SKIP] {code}/{strat}: {type(e).__name__}: {e}")
            continue
        if not r or not r.get("ok"):
            detail_lines.append(f"  [SKIP] {code}/{strat}: {r.get('message', '?') if r else 'no result'}")
            continue
        trades = r.get("trades") or []
        n_added = 0
        for idx, t in enumerate(trades):
            d = t.get("date")
            side = t.get("side")
            qty = int(t.get("size", 0))
            px = float(t.get("price", 0))
            if not d or side not in ("buy", "sell") or qty <= 0 or px <= 0:
                continue
            new_orders.append({
                "ts":       f"{d}T15:00:00",
                "code":     code,
                "name":     name,
                "side":     side,
                "quantity": qty,
                "price":    round(px, 4),
                "amount":   round(qty * px, 2),
                "status":   "dry_run",
                "order_id": f"seed_{code}_{idx}",
                "strategy": strat,
                "reason":   t.get("reason", ""),
            })
            n_added += 1
        detail_lines.append(f"  [OK] {code}/{strat}: 写入 {n_added} 笔成交")

    if not new_orders:
        return ("[WARN] 所有股票区间内都没产生成交 (策略全程 hold), orders 仍为空\n"
                + "\n".join(detail_lines))

    new_orders.sort(key=lambda x: x["ts"])

    # 重新读一遍 + 写回 (避免读到写之间被主循环改动)
    s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
    s["orders"] = new_orders
    s["_updated_at"] = datetime.now().isoformat(timespec="seconds")

    tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUTPUTS_LIVE_STATE)

    n_run_ok = sum(1 for d in detail_lines if d.startswith("  [OK]"))
    n_with_trade = sum(1 for d in detail_lines
                       if d.startswith("  [OK]") and not d.endswith("写入 0 笔成交"))
    return (f"[OK] 历史订单回填: 共写入 {len(new_orders)} 笔 "
            f"({start_date} ~ {end_date}, "
            f"{n_run_ok}/{len(positions)} 只票运行成功, {n_with_trade} 只产生成交)\n"
            + "\n".join(detail_lines))


class LiveSimRunner:
    """单例模拟盘/实盘控制器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = False
        self._loop = None
        self._cycle_count = 0
        self._last_cycle_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._dry_run = False
        self._router = None        # StrategyRouter -- 启动后才创建
        self._last_watch_stocks: Optional[List[str]] = None  # 最近一次启动时的合并监控列表

    # ------------------------------------------------------------------
    def status(self) -> dict:
        running = self._thread is not None and self._thread.is_alive()
        return {
            "running":        running,
            "cycle_count":    self._cycle_count,
            "last_cycle_at":  self._last_cycle_at,
            "last_error":     self._last_error,
            "dry_run":        self._dry_run,
            "watch_stocks":   list(self._last_watch_stocks or []),
        }

    # ------------------------------------------------------------------
    def start(self, watch_stocks: List[str], cycle_seconds: int = 60,
              dry_run: bool = False, init_positions: bool = False) -> str:
        """启动后台循环 (恒为实盘)

        Args:
            watch_stocks: 监控股票池
            cycle_seconds: 循环周期 (秒)
            dry_run: 已废弃 (恒为 False, 不再有模拟撮合)
            init_positions: 是否用 config 播种初仓 (默认 False: 实盘持仓以实际成交为准)
        """
        if self._thread is not None and self._thread.is_alive():
            return "[INFO] 引擎已在运行, 请先停止"

        from lib.paths import OUTPUTS_LIVE_STATE, setup_sys_path
        setup_sys_path()
        from live_trading.live_loop import LiveTradingLoop

        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))

        # 新模型: 无 default / no per_stock 路由, 实盘按策略独立评估 (每策略自己的选股池+持仓)
        self._router = None

        try:
            self._last_watch_stocks = list(watch_stocks)
            self._loop = LiveTradingLoop(
                watch_stocks=watch_stocks,
                capital=capital,
                state_file=str(OUTPUTS_LIVE_STATE),
                dry_run=dry_run,
                signal_evaluator=None,
                per_strategy_mode=True,
            )
            self._dry_run = dry_run
        except Exception as e:
            return f"[ERROR] 创建 LiveTradingLoop 失败: {e}"

        # 初始化持仓 (从配置)
        s = self._loop.state_store.load()
        s["trading_status"] = "RUNNING"
        if init_positions:
            s["positions"] = build_positions_from_config()
        # 持仓 cur_price 已用最新日 K close 刷新 -> 同步重算 today_pnl
        # (避免之前运行残留的 today_pnl 与 positions 表对不上)
        try:
            from live_trading.live_loop import calc_today_pnl
            today_pnl, today_pct = calc_today_pnl(s.get("positions", []), capital)
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pct
        except Exception as e:
            print(f"[WARN] 启动时重算 today_pnl 失败: {e}", flush=True)
        self._loop.state_store.save(s)

        # 历史信号回放: 信号表为空时, 把每只监控股票从 SIM_HISTORY_START_DATE
        # 至今的策略 buy/sell 信号回放进 state.signals, 用户首次打开就能看到历史
        # 失败不阻塞 start (回测引擎/MySQL/数据缺失都属于非致命)
        try:
            self._seed_historical_signals_if_empty(watch_stocks)
        except Exception as e:
            print(f"[WARN] 历史信号回放失败 (不影响主流程): {e}", flush=True)

        # 历史订单回填: orders 表为空时, 跑一遍回测引擎拿成交 trades, 写入 state.orders
        # 让 /review 流水卡有数据可看 (信号是"建议", 订单是"执行", 两者都要有)
        try:
            log = seed_historical_orders(force=False)
            print(log, flush=True)
        except Exception as e:
            print(f"[WARN] 历史订单回填失败 (不影响主流程): {e}", flush=True)

        # 历史资金曲线回放: 资金曲线没"今天之前的点"时, 把 [start_date, 昨天] 每个交易日
        # 按 mock_positions buy-and-hold 算的总资产% 写进 state.pnl_history,
        # 让"资金曲线"图启动就能看到 4-1 至今的趋势, 而不是只有今天那一段
        try:
            self._seed_historical_pnl_curve_if_empty()
        except Exception as e:
            print(f"[WARN] 历史资金曲线回放失败 (不影响主流程): {e}", flush=True)

        # 启动 worker
        self._stop_flag = False
        self._cycle_count = 0
        self._last_error = None

        def worker():
            while not self._stop_flag:
                # 非交易时段直接跳过 (不拉行情, 不算信号, 不下单)
                # 这样信号/订单只会在 9:30-11:30 + 13:00-15:00 出现
                if not is_a_share_trading_hour():
                    self._last_cycle_at = datetime.now().strftime("%H:%M:%S") + " (非盘中, 跳过)"
                else:
                    try:
                        self._loop.run_once()
                        self._cycle_count += 1
                        self._last_cycle_at = datetime.now().strftime("%H:%M:%S")
                        self._last_error = None
                    except Exception as e:
                        self._last_error = f"{type(e).__name__}: {e}"
                # 每 1 秒检查 stop_flag
                for _ in range(cycle_seconds):
                    if self._stop_flag:
                        break
                    time.sleep(1)

        self._thread = threading.Thread(target=worker, daemon=True,
                                         name="LiveSimRunner")
        self._thread.start()

        mode_str = "模拟模式 (dry-run, 不连券商)" if dry_run else "实盘模式 (真实下单!)"
        return (f"[OK] 已启动 -- {mode_str}\n"
                f"     合并后监控 {len(watch_stocks)} 只: {watch_stocks}, 周期={cycle_seconds}s, "
                f"初始持仓 {len(s.get('positions', []))} 只, 总资金 {capital:,.0f}\n"
                f"     实盘按策略独立评估 (每策略自己的选股池+持仓, 无默认策略)\n"
                f"     dashboard 每 5 秒自动刷新")

    # ------------------------------------------------------------------
    def stop(self) -> str:
        if self._thread is None:
            return "[INFO] 模拟盘未启动"
        self._stop_flag = True
        self._thread.join(timeout=5)
        msg = f"[OK] 已停止 -- 共跑了 {self._cycle_count} 轮"
        self._thread = None
        return msg

    # ------------------------------------------------------------------
    def trial_run(self) -> dict:
        """非交易时段也强制跑一次, 给用户看「现在各策略给的方向」.

        返回值:
            {
                "ok":        True/False,
                "message":   总结 (用户提示用)
                "summary":   {"buy": n, "sell": n, "hold": n, "total": n},
                "diagnoses": [{"code", "strategy", "side", "reason"}, ...]
            }

        实现说明:
            - 调 loop.run_once() 走完整流程 (含风控/下单), 信号写进 state.signals 表
            - 同时单独再跑一遍 router 收集每只股票的方向 (buy/sell/hold), 含 hold 也返回
              这样用户能直观看到「为什么没有信号 == 6 只全 hold」
            - 必须先 start() 过, 否则 self._loop 还没创建
        """
        if self._loop is None:
            return {
                "ok": False,
                "message": "[ERROR] 模拟盘未启动 (self._loop is None), 请先点「启动循环」",
                "summary": {}, "diagnoses": [],
            }

        # 先跑完整 loop (会写 signals/orders, 触发风控)
        try:
            self._loop.run_once()
            self._cycle_count += 1
            self._last_cycle_at = datetime.now().strftime("%H:%M:%S") + " (试算)"
            self._last_error = None
            loop_msg = f"[OK] 已试算 1 轮 (累计循环 {self._cycle_count})"
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            return {
                "ok": False,
                "message": f"[ERROR] 试算失败: {self._last_error}",
                "summary": {}, "diagnoses": [],
            }

        # 再跑一遍各策略 (仅诊断, 不写盘) 用于把 hold 也告诉用户
        diagnoses: List[dict] = []
        summary = {"buy": 0, "sell": 0, "hold": 0, "error": 0, "total": 0}
        try:
            capital = float(load_mock_config().get("capital", 1_000_000))
            from lib.selection_engine import iter_strategy_signal_targets
            from lib.strategy_registry import get_strategy
            targets = iter_strategy_signal_targets() or \
                [(None, c) for c in (self._last_watch_stocks or merge_watch_codes())]
            for sname, code in targets:
                try:
                    meta = get_strategy(sname) if sname else None
                    r = (meta.evaluator(code, self._loop.market, capital)
                         if meta is not None else
                         {"side": "hold", "strategy": "?", "reason": "策略未启用/未注册"})
                    if not r.get("strategy"):
                        r["strategy"] = sname or "?"
                except Exception as e:
                    r = {"side": "error", "strategy": sname or "?",
                         "reason": f"{type(e).__name__}: {e}"}
                side = r.get("side", "hold")
                summary[side] = summary.get(side, 0) + 1
                summary["total"] += 1
                diagnoses.append({
                    "code":     code,
                    "strategy": r.get("strategy", "?"),
                    "side":     side,
                    "reason":   r.get("reason", ""),
                })
        except Exception as e:
            # 诊断失败不影响主流程
            print(f"[WARN] trial_run diagnose error: {e}", flush=True)

        msg = (f"{loop_msg} -- 监控 {summary['total']} 只: "
               f"buy={summary['buy']}, sell={summary['sell']}, "
               f"hold={summary['hold']}"
               + (f", error={summary['error']}" if summary.get("error") else ""))
        return {
            "ok": True,
            "message": msg,
            "summary": summary,
            "diagnoses": diagnoses,
        }

    # ------------------------------------------------------------------
    def apply_strategy_config(self, cfg: Optional[dict] = None,
                          default: Optional[str] = None,
                          per_stock: Optional[dict] = None) -> str:
        """保存多策略选股配置 (config/strategy_selection.yaml) 并让选股生效.

        新模型: cfg 为 {universe, strategies: [...]}, 无 default/per_stock 概念。
        兼容旧调用 (default=, per_stock=): 把 per_stock 的 {code->strategy} 折算成
        各策略的 code_list 实例, default 直接忽略(无默认策略)。
        """
        from lib.strategy_runner import save_selection_config, load_selection_config
        if per_stock is not None:
            cfg = _per_stock_to_selection_cfg(per_stock)
        if cfg is None:
            cfg = load_selection_config()
        save_selection_config(cfg)
        n = len(cfg.get("strategies", []))
        return f"[OK] 多策略选股配置已保存 -- {n} 个策略实例 (无默认策略)"

    # ------------------------------------------------------------------
    def _seed_historical_pnl_curve_if_empty(self) -> None:
        """启动时把 [SIM_HISTORY_START_DATE, 昨天] 的每日资金曲线点回放进 state.pnl_history.

        - 仅在 pnl_history 里没有"早于今天"的点时跑 (今天的盘中分时数据保留不动)
        - 用 mock_positions (vol/cost) + 每日 close 算每天的"总资产 vs 初始资金"百分比:
            asset_d = sum(vol_i * close_i[d]) + cash
            pnl_pct = (asset_d - capital) / capital
            cash = capital - sum(vol_i * cost_i)   (一直不动, 因为我们目前没成交)
        - ts 取当日 15:00:00 (与历史信号 ts 同格), 与今天的盘中分时点拼成完整曲线
        - 失败不影响启动
        """
        s = self._loop.state_store.load()
        hist = s.get("pnl_history") or []
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        # 是否已有"今天之前"的点 -> 有就不再补 (避免重启重复 seed)
        has_pre_today = any((p.get("ts", "") < today_str) for p in hist)
        if has_pre_today:
            return

        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))
        positions_cfg = cfg.get("positions", []) or []
        if not positions_cfg:
            return

        # 现金 = 初始资金 - 持仓 cost 占用 (假定区间内无成交, sim 引擎非盘中没跑)
        cost_used = sum(int(p.get("volume", 0)) * float(p.get("cost", 0))
                        for p in positions_cfg)
        cash = capital - cost_used

        try:
            from lib.backtest_data import load_daily_kline
        except Exception as e:
            print(f"[WARN] pnl_history seed: import backtest_data 失败 -> {e}", flush=True)
            return

        start_date = os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")

        # 1) 拉每只票从 start_date 至今的日 K close
        # 2) 取所有日期的并集 (按交易日齐对齐), 没数据的日 forward fill
        import pandas as pd
        close_dfs = {}
        for p in positions_cfg:
            code = p.get("code", "")
            try:
                df = load_daily_kline(code, start_date=start_date, end_date=today_str)
            except Exception as e:
                print(f"[WARN] pnl_history seed: 拉 {code} 失败: {e}", flush=True)
                continue
            if df is None or df.empty:
                continue
            close_dfs[code] = df["close"]

        if not close_dfs:
            return

        # 合并, ffill 防节假日空档 (虽然交易日都有, 容错)
        all_closes = pd.concat(close_dfs, axis=1).sort_index().ffill()

        new_points: List[dict] = []
        for ts, row in all_closes.iterrows():
            d_str = ts.strftime("%Y-%m-%d")
            # 只回放 < today (今天用真实盘中数据)
            if d_str >= today_str:
                continue
            mv = 0.0
            for p in positions_cfg:
                code = p.get("code", "")
                vol = int(p.get("volume", 0))
                px = row.get(code)
                if px is None or pd.isna(px):
                    continue
                mv += vol * float(px)
            asset = cash + mv
            pnl = asset - capital
            pct = pnl / capital if capital > 0 else 0.0
            # ts 用 14:59:00 而非 15:00:00 -- 避免踩在 plotly rangebreak [15, 9.5] 边界
            # 上被当作非交易时段过滤掉
            new_points.append({
                "ts":      f"{d_str}T14:59:00",
                "pnl":     round(pnl, 2),
                "pnl_pct": round(pct, 4),
            })

        if not new_points:
            return

        # 把回放点拼到 pnl_history 前面 (今天的分时点保持原顺序)
        merged = new_points + hist
        # 与 update_pnl 一致, 末位保留 500 条
        s = self._loop.state_store.load()
        s["pnl_history"] = merged[-500:]
        self._loop.state_store.save(s)
        print(f"[OK] 历史资金曲线回放: 写入 {len(new_points)} 个交易日点 "
              f"({start_date} ~ 昨天)", flush=True)

    # ------------------------------------------------------------------
    def _seed_historical_signals_if_empty(self, watch_stocks: List[str]) -> None:
        """启动时把 [SIM_HISTORY_START_DATE, today] 的策略信号回放进 state.signals

        - 仅在 state.signals 当前为空时跑 (避免重启后重复回放)
        - 调 backtest_engine.run_backtest 拿每只股票按其路由策略跑出的所有 buy/sell
          (撮合规则一致, 但本方法不写 trades, 只把信号 ts/code/side/strategy/reason
          按时间升序 append 到 state.signals)
        - 起始日期: 环境变量 SIM_HISTORY_START_DATE (默认 2026-04-01, 与 mock_positions
          初始持仓的 cost 日对齐)
        """
        # 已有信号 -> 不重放, 避免每次重启都重复
        s = self._loop.state_store.load()
        if s.get("signals"):
            return
        if not watch_stocks:
            return

        from datetime import date
        start_date = os.environ.get("SIM_HISTORY_START_DATE", "2026-04-01")
        end_date = date.today().strftime("%Y-%m-%d")

        # 延迟 import: 避免 backtest 依赖在不需要时强制加载
        try:
            from lib.backtest_engine import run_backtest
        except Exception as e:
            print(f"[WARN] 历史回放: import backtest_engine 失败 -> {e}", flush=True)
            return

        all_sigs: List[dict] = []
        for code in watch_stocks:
            strat = (self._router.per_stock.get(code) if self._router else None) \
                    or (self._router.default if self._router else "macd_1d")
            try:
                r = run_backtest(stock_code=code, strategy_name=strat,
                                 start_date=start_date, end_date=end_date)
            except Exception as e:
                print(f"[WARN] 历史回放 {code}/{strat} 异常: {e}", flush=True)
                continue
            if not r or not r.get("ok"):
                continue
            for sig in r.get("signals", []) or []:
                side = sig.get("side")
                if side not in ("buy", "sell"):
                    continue
                # 信号在 K_i 收盘出 -> 用日 K 收盘时刻 15:00:00 作为 ts (与盘中 ts 同格)
                d = sig.get("date")
                if not d:
                    continue
                all_sigs.append({
                    "ts":       f"{d}T15:00:00",
                    "code":     code,
                    "side":     side,
                    "strategy": strat,
                    "reason":   sig.get("reason", ""),
                })

        if not all_sigs:
            return

        all_sigs.sort(key=lambda x: x["ts"])
        # 与 append_signal 一致, 末位保留 100 条
        s = self._loop.state_store.load()
        s["signals"] = (s.get("signals") or []) + all_sigs
        s["signals"] = s["signals"][-100:]
        self._loop.state_store.save(s)
        print(f"[OK] 历史信号回放: 写入 {len(all_sigs)} 条 "
              f"({start_date} ~ {end_date}, {len(watch_stocks)} 只)", flush=True)

    # ------------------------------------------------------------------
    def clear_history(self) -> str:
        """清空 events / signals / orders / pnl_history (持仓不动)
        用于清掉之前测试遗留的非盘中脏数据
        """
        from lib.paths import OUTPUTS_LIVE_STATE
        import json, os
        if not OUTPUTS_LIVE_STATE.exists():
            return "[ERROR] live_state.json 不存在"
        try:
            s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        cleared = (len(s.get("events", [])) + len(s.get("signals", []))
                   + len(s.get("orders", [])) + len(s.get("pnl_history", [])))
        s["events"]      = []
        s["signals"]     = []
        s["orders"]      = []
        s["pnl_history"] = []
        s["today_pnl"]   = 0
        s["today_pnl_pct"] = 0
        s["_updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, OUTPUTS_LIVE_STATE)
        return f"[OK] 已清空 {cleared} 条历史 (events/signals/orders/pnl), 持仓保留"

    # ------------------------------------------------------------------
    def reset_positions(self) -> str:
        """重新读 config 并覆盖 state.positions (不动 events / signals / orders)"""
        from lib.paths import OUTPUTS_LIVE_STATE
        import json, os
        if not OUTPUTS_LIVE_STATE.exists():
            return "[ERROR] live_state.json 不存在, 先启动一次模拟盘"
        try:
            s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        new_positions = build_positions_from_config()
        s["positions"] = new_positions
        cfg = load_mock_config()
        capital = float(cfg.get("capital", 1_000_000))
        s["capital"] = capital
        # 重算 today_pnl, 与持仓表对齐 (新 cur_price 已从最新日 K close 拿了)
        try:
            from lib.paths import setup_sys_path
            setup_sys_path()
            from live_trading.live_loop import calc_today_pnl
            today_pnl, today_pct = calc_today_pnl(new_positions, capital)
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pct
        except Exception as e:
            print(f"[WARN] reset_positions 重算 today_pnl 失败: {e}", flush=True)
        s["_updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = OUTPUTS_LIVE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, OUTPUTS_LIVE_STATE)
        return f"[OK] 重置持仓 -- 共 {len(new_positions)} 只, 来源 config/mock_positions.yaml"


# ============================================================
# cli -- 不启动主循环, 只跑历史订单回填 (用法见 --help)
# ============================================================
if __name__ == "__main__":
    import argparse
    import sys
    # 直接跑 python lib/live_simulator.py 时, lib 不在 sys.path -- 把项目根加进去
    _PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from lib.paths import setup_sys_path
    setup_sys_path()

    parser = argparse.ArgumentParser(
        description="LiveSimRunner cli -- 历史订单回填工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("用法:\n"
                "  python lib/live_simulator.py --backfill-orders\n"
                "        把 SIM_HISTORY_START_DATE (默认 2026-04-01) 至今的策略成交\n"
                "        写到 outputs/live_state.json/orders, 让 /review 流水卡有数据\n"
                "  python lib/live_simulator.py --backfill-orders --force\n"
                "        即使 orders 已有也强制覆盖 (重生成)\n"))
    parser.add_argument("--backfill-orders", action="store_true",
                        help="跑一遍回测引擎, 把成交流水写入 live_state.json/orders")
    parser.add_argument("--force", action="store_true",
                        help="即使 orders 已有也强制覆盖")
    args = parser.parse_args()

    if args.backfill_orders:
        log = seed_historical_orders(force=args.force)
        print(log)
        sys.exit(0)

    parser.print_help()
