# -*- coding: utf-8 -*-
# 开盘买入窗口任务 -- 弱转强选股池 09:30:00-09:45:00 每股一个循环监控任务自动买入
"""
OpenBuyWindowRunner -- 针对弱转强策略当日选出的股票, 在开盘窗口内高频监控买入。

模型 (每股一个循环监控任务):
    - 启动: 为当日每只弱转强候选股各创建一个独立后台监控线程 (每股一个任务)
    - 循环: 每只股票的线程在自己窗口内每 30 秒评估一次 (tick/分钟K -> 策略判定)
    - 触发: 满足买入条件 -> 发起自动下单指令 (复用 _handle_signal 风控/下单/记账)
            无论下单成功与否, 该股票的监控任务立即结束
    - 超时: 到 09:45 仍未触发买入 -> 该股票的监控任务也结束 (放弃当天买入)
    - 并行: 各股评估/下单通过 _order_lock 串行化, 避免并发写 live_state.json

状态落盘到 outputs/live_open_buy_window.json, 供实盘监控页「窗口任务进度」卡片展示:
    stocks[code] = {code, name, strategy, strategy_label, open_price, day_low, cycles,
                    task_status, bought, buy_reason, not_buy_reason, order_status, checked_at}
    task_status: monitoring(监控中) / ordered(已下单·任务结束) /
                 window_end(窗口结束未买) / error(异常结束)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional

log = logging.getLogger("open-buy-window")

WINDOW_START = (9, 30, 0)   # 窗口开始 09:30:00 (含)
WINDOW_END = (9, 45, 0)     # 窗口结束 09:45:00 (含), 之后放弃当天买入
CYCLE_SECONDS = 30        # 每股每轮间隔 (秒)
STRATEGY = "weak_to_strong"

STATE_FILE_NAME = "live_open_buy_window.json"

# 任务状态 -> 中文
_TASK_STATUS_TEXT = {
    "monitoring":  "监控中",
    "ordered":     "已下单",
    "window_end":  "窗口结束",
    "error":       "异常",
}


def in_buy_window(now: Optional[datetime] = None) -> bool:
    """当前是否处于开盘买入窗口 (周一至周五 09:30:00-09:45:00)"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(*WINDOW_START) <= t <= dt_time(*WINDOW_END)


def _load_buy_position(strategy: str = STRATEGY) -> dict:
    """读取指定策略实例 (默认 weak_to_strong) 的买入仓位管理配置:
    - qty_enabled / qty:     启用固定买入股数 (最低100股)
    - max_enabled/max_percent: 启用单只买入金额不高于账号总资金比例 (%)
    - max_daily:             当日最多买入股票只数 (基于涨幅从高到低取)
    买入量优先级: 固定股数 > 金额仓位 > 退回100股
    缺省: qty_enabled=True, qty=100, max_enabled=False, max_percent=20, max_daily=10"""
    try:
        from lib.strategy_runner import load_selection_config
        cfg = load_selection_config()
        inst = next((s for s in cfg.get("strategies", [])
                     if (s.get("name") or "") == strategy), None)
        buy = ((inst or {}).get("trade") or {}).get("buy") or {}
        qty = buy.get("qty")
        qty_enabled = buy.get("qty_enabled")
        mp = buy.get("max_percent")
        max_enabled = buy.get("max_enabled")
        md = buy.get("max_daily")
        return {
            "qty_enabled": bool(qty_enabled),
            "qty": int(qty) if str(qty or "") not in ("", "None") else 100,
            "max_enabled": bool(max_enabled),
            "max_percent": float(mp) if mp not in (None, "") else 20.0,
            "max_daily": int(md) if md not in (None, "") else 10,
        }
    except Exception:
        return {"qty_enabled": True, "qty": 100, "max_enabled": False,
                "max_percent": 20.0, "max_daily": 10}


class OpenBuyWindowRunner:
    """开盘买入窗口控制器 (单例): 每股一个后台监控线程"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._threads: Dict[str, threading.Thread] = {}   # code -> 该股的监控线程
        self._refresh_thread: Optional[threading.Thread] = None  # 后台补候选线程
        self._stop_flag = False
        self._loop = None
        self._last_error: Optional[str] = None
        self._last_candidates: List[str] = []
        self._candidate_names: Dict[str, str] = {}
        self._candidate_strategies: Dict[str, str] = {}   # code -> 来源策略名 (来自的策略)
        self._stocks: Dict[str, dict] = {}    # code -> 判定详情 (开盘价/已买·未买原因/任务状态)
        self._orders: List[dict] = []         # 窗口内下单结果
        self._run_date: str = ""              # 本次运行的日期 (跨天自动清空旧详情)
        self._lock = threading.RLock()        # 保护 _threads/_stocks/_orders 等内存态
        self._order_lock = threading.Lock()   # 串行化 评估+下单+state 落盘 (防并发覆盖)

    # ------------------------------------------------------------------
    @staticmethod
    def _state_file():
        from lib.paths import OUTPUTS_DIR
        return OUTPUTS_DIR / STATE_FILE_NAME

    # ------------------------------------------------------------------
    def status(self) -> dict:
        """内存态 (给 scheduler 等进程内调用)"""
        with self._lock:
            running = any(t.is_alive() for t in self._threads.values())
            return {
                "running":        running,
                "active_count":   sum(1 for d in self._stocks.values()
                                      if d.get("task_status") == "monitoring"),
                "last_error":     self._last_error,
                "candidates":     list(self._last_candidates),
            }

    @classmethod
    def load_status(cls) -> dict:
        """读最近一次落盘的状态 (给 routes/live.py 等跨请求读取)"""
        f = cls._state_file()
        if f is None or not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_status(self) -> None:
        """把窗口运行进度 + 每股判定详情 + 成交记录落盘 (写文件在锁内, 防并发覆盖)"""
        with self._lock:
            running = any(t.is_alive() for t in self._threads.values())
            if running:
                status_text = "运行中"
            elif self._run_date:
                status_text = "已结束"
            else:
                status_text = "未启动"
            active = sum(1 for d in self._stocks.values()
                         if d.get("task_status") == "monitoring")
            ordered = sum(1 for d in self._stocks.values()
                          if d.get("task_status") == "ordered")
            window_end = sum(1 for d in self._stocks.values()
                             if d.get("task_status") == "window_end")
            error = sum(1 for d in self._stocks.values()
                        if d.get("task_status") == "error")
            data = {
                "date":           datetime.now().strftime("%Y-%m-%d"),
                "running":        running,
                "running_text":   status_text,
                "active_count":   active,
                "ordered_count":  ordered,
                "window_end_count": window_end,
                "error_count":    error,
                "cycle_seconds":  CYCLE_SECONDS,
                "last_cycle_at":  self._last_cycle_at(),
                "last_error":     self._last_error,
                "window_start":   f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}:{WINDOW_START[2]:02d}",
                "window_end":     f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}:{WINDOW_END[2]:02d}",
                "candidates":     list(self._last_candidates),
                # 监控中在前, 再按代码排
                "stocks": sorted(
                    self._stocks.values(),
                    key=lambda x: (0 if x.get("task_status") == "monitoring" else 1,
                                   x.get("code") or ""),
                ),
                "orders": list(self._orders)[-200:],
            }
            try:
                f = self._state_file()
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, f)
            except Exception as e:
                log.warning("[WINDOW] 状态落盘失败: %s", e)

    def _last_cycle_at(self) -> Optional[str]:
        """最近一次任意股票被检查的时间"""
        ts = None
        for d in self._stocks.values():
            if d.get("checked_at") and (ts is None or d["checked_at"] > ts):
                ts = d["checked_at"]
        return ts

    # ------------------------------------------------------------------
    def start(self) -> str:
        """为当日每只弱转强候选各启动一个独立监控任务 (线程)"""
        with self._lock:
            if any(t.is_alive() for t in self._threads.values()):
                return "[INFO] 开盘买入窗口任务已在运行, 跳过"
        if datetime.now().time() > dt_time(*WINDOW_END):
            return "[INFO] 已过 09:45:00 买入窗口, 不启动"
        try:
            from lib.live_simulator import load_mock_config
            from lib.paths import OUTPUTS_LIVE_STATE, setup_sys_path
            setup_sys_path()
            from live_trading.live_loop import LiveTradingLoop
            capital = float(load_mock_config().get("capital", 1_000_000))
            self._loop = LiveTradingLoop(
                watch_stocks=[],
                capital=capital,
                state_file=str(OUTPUTS_LIVE_STATE),
                dry_run=False,
                signal_evaluator=None,
                per_strategy_mode=False,
            )
        except Exception as e:
            return f"[ERROR] 创建 LiveTradingLoop 失败: {e}"

        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            if today != self._run_date:
                # 新的一天: 清空上一日的候选详情 / 成交记录
                self._stocks.clear()
                self._orders.clear()
                self._run_date = today
            self._stop_flag = False
            self._last_error = None

        rows = self._today_candidate_rows()
        if not rows:
            self._save_status()
            return "[INFO] 当日选股池无候选, 不启动监控任务"
        with self._lock:
            self._last_candidates = [str(r["stock_code"]) for r in rows]
            self._candidate_names = {str(r["stock_code"]): (r.get("name") or "")
                                     for r in rows}
            self._candidate_strategies = {str(r["stock_code"]): str(r.get("strategy") or STRATEGY)
                                          for r in rows}

        # 每股一个独立监控线程
        started = 0
        for r in rows:
            code = str(r["stock_code"])
            with self._lock:
                self._detail(code)   # 初始化每股任务槽位
                t = threading.Thread(target=self._monitor_stock, args=(code,),
                                     daemon=True, name=f"OpenBuy-{code}")
                self._threads[code] = t
            t.start()
            started += 1
        # 后台候选刷新: 窗口期内周期重扫「当日监控池」候选, 为 start 之后
        # (如 09:26 弱转强选股) 才选出的新候选补充启动监控任务
        if not (self._refresh_thread and self._refresh_thread.is_alive()):
            self._refresh_thread = threading.Thread(
                target=self._refresh_candidates_loop,
                daemon=True, name="OpenBuy-refresh")
            self._refresh_thread.start()
        self._save_status()
        return (f"[OK] 开盘买入窗口已启动 -- 已为 {started} 只候选各创建 1 个监控任务, "
                f"每 {CYCLE_SECONDS}s 判定, 触发买入或到 09:45 即结束")

    # ------------------------------------------------------------------
    def stop(self) -> str:
        """停止所有每股监控任务"""
        with self._lock:
            if not self._threads:
                return "[INFO] 开盘买入窗口任务未运行"
            self._stop_flag = True
            threads = list(self._threads.values())
        for t in threads:
            t.join(timeout=5)
        with self._lock:
            self._threads.clear()
        self._save_status()
        return f"[OK] 已停止 -- {len(threads)} 个监控任务已结束"

    # ------------------------------------------------------------------
    def _today_candidate_rows(self) -> List[dict]:
        """当日买入候选 (完整行, 含代码/名称/策略).

        来源 = 监控池 watch_pool.yaml (手动添加的股票):
          - 只买监控池里当前存在的股票 (监控池有啥买啥);
          - 按 bindings 绑定策略评估买入, 无绑定 -> 回退默认 STRATEGY。
        """
        from lib.live_simulator import load_watch_pool
        manual = load_watch_pool()
        manual_codes = [str(c) for c in (manual.get("codes") or [])
                        if str(c).strip()]
        if not manual_codes:
            return []
        try:
            from lib.selection_engine import _stock_name_map
            base = _stock_name_map()
        except Exception:
            base = {}
        manual_bind = manual.get("bindings") or {}
        today = datetime.now().strftime("%Y-%m-%d")
        result = []
        for mc in manual_codes:
            result.append({
                "stock_code": mc,
                "name": base.get(mc, ""),
                "strategy": str(manual_bind.get(mc) or STRATEGY),
                "trade_date": today,
            })
        return result

    def _today_candidates(self) -> List[str]:
        rows = self._today_candidate_rows()
        self._candidate_names = {str(r["stock_code"]): (r.get("name") or "")
                                 for r in rows}
        self._candidate_strategies = {str(r["stock_code"]): str(r.get("strategy") or STRATEGY)
                                      for r in rows}
        return [str(r["stock_code"]) for r in rows]

    # ------------------------------------------------------------------
    def _refresh_candidates_loop(self) -> None:
        """后台补候选: 窗口内每 5s 重扫当日监控池候选.

        对窗口内新添加进监控池 (watch_pool.yaml) 的股票补充启动监控任务
        (已 ordered/window_end/在跑 的跳过)。
        """
        while not self._stop_flag:
            if datetime.now().time() > dt_time(*WINDOW_END):
                break
            if in_buy_window():
                try:
                    rows = self._today_candidate_rows()
                except Exception:
                    rows = []
                spawned = 0
                with self._lock:
                    for r in rows:
                        code = str(r["stock_code"])
                        t = self._threads.get(code)
                        if t is not None and t.is_alive():
                            continue
                        d = self._stocks.get(code)
                        if d and d.get("task_status") in ("ordered", "window_end"):
                            continue
                        self._candidate_names[code] = (r.get("name") or
                                                       self._candidate_names.get(code, ""))
                        self._candidate_strategies[code] = str(
                            r.get("strategy") or self._candidate_strategies.get(code, STRATEGY))
                        self._detail(code)
                        nt = threading.Thread(target=self._monitor_stock, args=(code,),
                                              daemon=True, name=f"OpenBuy-{code}")
                        self._threads[code] = nt
                        nt.start()
                        spawned += 1
                    if spawned:
                        self._save_status()
            for _ in range(5):
                if self._stop_flag:
                    break
                time.sleep(1)

    # ------------------------------------------------------------------
    def _monitor_stock(self, code: str) -> None:
        """单只股票的循环监控任务:
        窗口内每 30 秒评估一次; 触发买入下单(无论成败)或窗口结束(09:45)即结束本任务"""
        from lib.strategy_registry import get_strategy
        # 每股用其所属策略的 evaluate 判断买入 (支持多策略候选)
        own = self._candidate_strategies.get(code)
        meta = get_strategy(own if own else STRATEGY)
        if meta is None:
            meta = get_strategy(STRATEGY)
        try:
            while not self._stop_flag:
                if datetime.now().time() < dt_time(*WINDOW_START):
                    # 窗口开始 (09:30:00) 起, 每轮即评估
                    for _ in range(2):
                        if self._stop_flag:
                            break
                        time.sleep(1)
                    continue
                if not in_buy_window():
                    # 09:45 后仍未触发买入 -> 结束该股任务
                    self._finish_window_end(code)
                    break
                try:
                    if self._evaluate_and_order(code, meta):
                        break   # 已下单, 无论成败结束本任务
                except Exception as e:
                    with self._lock:
                        d = self._stocks.get(code)
                        if d is not None:
                            d["task_status"] = "error"
                            d["task_status_text"] = _TASK_STATUS_TEXT["error"]
                            d["not_buy_reason"] = f"{type(e).__name__}: {e}"
                        self._last_error = f"{type(e).__name__}: {e}"
                    log.exception("[WINDOW] %s 本轮异常", code)
                # 每 1 秒检查 stop_flag (与 LiveSimRunner worker 同款)
                for _ in range(CYCLE_SECONDS):
                    if self._stop_flag:
                        break
                    time.sleep(1)
        finally:
            with self._lock:
                self._threads.pop(code, None)
            self._save_status()

    # ------------------------------------------------------------------
    def _evaluate_and_order(self, code: str, meta) -> bool:
        """单股单轮: 串行评估 -> 符合买入即下单 (无论成败, 任务结束).

        返回 True = 本股监控任务应结束 (已下单).
        """
        with self._order_lock:
            now_s = datetime.now().strftime("%H:%M:%S")
            with self._lock:
                detail = self._detail(code)
                detail["checked_at"] = now_s
                detail["cycles"] = int(detail.get("cycles") or 0) + 1
                detail["open_price"] = self._today_open_price(code) or detail.get("open_price")
                detail["day_low"] = self._today_day_low(code) or detail.get("day_low")
                if detail.get("task_status") in ("ordered", "window_end", "error"):
                    return True   # 已结束的任务不再处理

            # 熔断 / 暂停时不买入 (任务继续监控, 等恢复)
            s = self._loop.state_store.load()
            if s.get("trading_status") in ("HALTED", "PAUSED"):
                with self._lock:
                    detail["not_buy_reason"] = f"交易状态 {s.get('trading_status')}, 暂停买入"
                return False

            try:
                result = meta.evaluator(code, self._loop.market, self._loop.capital)
            except Exception as e:
                log.warning("[WINDOW] 评估异常 %s: %s", code, e)
                with self._lock:
                    detail["not_buy_reason"] = f"评估异常: {type(e).__name__}: {e}"
                return False
            if not result or result.get("side") != "buy":
                with self._lock:
                    detail["not_buy_reason"] = (result or {}).get("reason", "") or "未触发买入"
                return False

            # 触发买入 -> 发起自动下单 (无论成败, 本任务结束)
            with self._lock:
                detail["buy_reason"] = result.get("reason", "")
                detail["not_buy_reason"] = ""
            self._place_order(code, result.get("reason", ""), self._candidate_strategies.get(code))
            return True

    # ------------------------------------------------------------------
    def _buy_quantity(self, code: str, strategy: str = STRATEGY) -> int:
        """按买入仓位管理计算单只买入数量 (整手100股, 最少100股).

        优先级: 固定股数(qty_enabled) > 金额仓位(max_enabled) > 退回100股。
        - 固定股数: 返回配置的 qty (向下取整到整手, 最低100)
        - 金额仓位: quantity = floor(总资金 × max_percent% / 现价 / 100) × 100
        - 拉不到价格/异常 -> 退回 100 股
        """
        try:
            position = _load_buy_position(strategy)
            # 模式1: 固定股数
            if position.get("qty_enabled"):
                qty = int(position.get("qty") or 100)
                if qty < 100:
                    qty = 100
                return int(qty // 100) * 100
            # 模式2: 金额仓位
            if position.get("max_enabled"):
                max_percent = float(position.get("max_percent") or 20.0)
                capital = float(self._loop.capital or 0)
                if capital > 0:
                    price = None
                    try:
                        ticks = self._loop.market.get_full_ticks([code])
                        tick = (ticks or {}).get(code)
                        if tick:
                            price = float(tick.get("lastPrice") or 0)
                            if price <= 0:
                                price = float(tick.get("open") or 0)
                    except Exception:
                        price = None
                    if not price or price <= 0:
                        price = self._today_open_price(code)
                    if price and price > 0:
                        budget = capital * (max_percent / 100.0)
                        lots = int(budget / (price * 100))
                        if lots < 1:
                            lots = 1
                        return int(lots * 100)
            return 100
        except Exception:
            return 100

    # ------------------------------------------------------------------
    def _place_order(self, code: str, reason: str, strategy: Optional[str] = None) -> dict:
        """下单 (必须在 _order_lock 保护下调用):
        复用 LiveTradingLoop._handle_signal 风控/下单/记账链路, 结果落盘并结束该股任务"""
        strategy = strategy or STRATEGY
        s = self._loop.state_store.load()
        sig = {"code": code, "side": "buy", "strategy": strategy, "reason": reason}
        s["signals"] = s.get("signals", [])
        s["orders"] = s.get("orders", [])
        s["signals"].append({**sig, "ts": datetime.now().isoformat(timespec="seconds")})
        log.info("[WINDOW] 买入信号 -> %s (%s)", code, reason)
        order_result = self._loop._handle_signal(s, sig, quantity=self._buy_quantity(code, strategy))
        s["orders"].append({**order_result, "ts": datetime.now().isoformat(timespec="seconds")})
        s["signals"] = s["signals"][-100:]
        s["orders"] = s["orders"][-200:]
        self._loop.state_store.save(s)
        # 买入成交一次性落库 trade_buy_record (幂等, 便于持久化保存)
        try:
            from lib.selection_store import backfill_buy_records_from_orders
            backfill_buy_records_from_orders(s["orders"])
        except Exception:
            pass

        low = self._today_day_low(code)   # 任务结束前最后刷新一次当日最低
        with self._lock:
            d = self._stocks.get(code) or {}
            if low:
                d["day_low"] = low
            d["task_status"] = "ordered"
            d["task_status_text"] = _TASK_STATUS_TEXT["ordered"]
            d["order_status"] = order_result.get("status", "?")
            # bought 仅表示下单成功; 下单失败也结束本任务 (bought=0)
            d["bought"] = 1 if order_result.get("status") == "submitted" else 0
            if d["bought"]:
                d["not_buy_reason"] = ""
            else:
                d["not_buy_reason"] = (f"下单{order_result.get('status', '失败')}: "
                                       f"{order_result.get('reason', '')}")
            self._orders.append({
                "code": code,
                "name": self._candidate_names.get(code, ""),
                "side": "buy",
                "status": order_result.get("status", "?"),
                "reason": order_result.get("reason", ""),
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
        log.info("[WINDOW] %s 下单结果: %s (%s)", code,
                 order_result.get("status", "?"), order_result.get("reason", ""))
        return order_result

    # ------------------------------------------------------------------
    def _finish_window_end(self, code: str) -> None:
        """窗口结束(09:45)仍未触发买入 -> 结束该股监控任务"""
        low = self._today_day_low(code)   # 任务结束前最后刷新一次当日最低
        with self._lock:
            d = self._stocks.get(code)
            if d is not None and d.get("task_status") != "ordered":
                d["task_status"] = "window_end"
                d["task_status_text"] = _TASK_STATUS_TEXT["window_end"]
                if low:
                    d["day_low"] = low
                d["not_buy_reason"] = d.get("not_buy_reason") or "窗口结束(09:45)未触发买入"
                d["checked_at"] = datetime.now().strftime("%H:%M:%S")

    # ------------------------------------------------------------------
    def _today_open_price(self, code: str) -> Optional[float]:
        """今日开盘价 (取日K最后一根 open; 拉不到返回 None)"""
        try:
            df = self._loop.market.get_recent_kline(code, "1d", 2)
            if df is not None and len(df) >= 1:
                return float(df.iloc[-1]["open"])
        except Exception:
            pass
        return None

    def _today_day_low(self, code: str) -> Optional[float]:
        """监控最低价: 开盘买入窗口(09:30:00-09:45:00)时间段内的 1 分钟 K 的 low 最小值;
        窗口外无数据 -> 返回 None (前端显示 '-')"""
        try:
            import pandas as pd
            df = self._loop.market.get_recent_kline(code, "1m", 300)
            if df is None or len(df) < 1:
                return None
            idx = pd.to_datetime(df.index)
            today0 = pd.Timestamp.now().normalize()
            s_lo, e_lo = dt_time(*WINDOW_START), dt_time(*WINDOW_END)
            t = idx.time
            mask = (idx >= today0) & (t >= s_lo) & (t <= e_lo)
            if mask.any():
                return round(float(df.loc[mask, "low"].min()), 3)
        except Exception:
            pass
        return None

    def _detail(self, code: str) -> dict:
        """取/建某只候选的判定详情槽位 (锁内调用)"""
        d = self._stocks.get(code)
        if d is None:
            strategy = self._candidate_strategies.get(code, STRATEGY)
            strategy_label = strategy
            try:
                from lib.strategy_registry import get_strategy
                meta = get_strategy(strategy)
                if meta is not None and meta.label:
                    strategy_label = meta.label
            except Exception:
                pass
            d = {
                "code": code,
                "name": self._candidate_names.get(code, ""),
                "strategy": strategy,
                "strategy_label": strategy_label,
                "open_price": None,
                "day_low": None,
                "cycles": 0,
                "task_status": "monitoring",
                "task_status_text": _TASK_STATUS_TEXT["monitoring"],
                "order_status": None,
                "bought": 0,
                "buy_reason": "",
                "not_buy_reason": "",
                "checked_at": None,
            }
            self._stocks[code] = d
        return d
