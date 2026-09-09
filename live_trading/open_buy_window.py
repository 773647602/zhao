# -*- coding: utf-8 -*-
# 开盘买入窗口任务 -- 弱转强选股池 09:29-09:35 每股一个循环监控任务自动买入
"""
OpenBuyWindowRunner -- 针对弱转强策略当日选出的股票, 在开盘窗口内高频监控买入。

模型 (每股一个循环监控任务):
    - 启动: 为当日每只弱转强候选股各创建一个独立后台监控线程 (每股一个任务)
    - 循环: 每只股票的线程在自己窗口内每 15 秒评估一次 (tick/分钟K -> 策略判定)
    - 触发: 满足买入条件 -> 发起自动下单指令 (复用 _handle_signal 风控/下单/记账)
            无论下单成功与否, 该股票的监控任务立即结束
    - 超时: 到 09:35 仍未触发买入 -> 该股票的监控任务也结束 (放弃当天买入)
    - 并行: 各股评估/下单通过 _order_lock 串行化, 避免并发写 live_state.json

状态落盘到 outputs/live_open_buy_window.json, 供实盘监控页「窗口任务进度」卡片展示:
    stocks[code] = {code, name, open_price, cycles, task_status,
                    bought, buy_reason, not_buy_reason, order_status, checked_at}
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

WINDOW_START = (9, 29)     # 窗口开始 (含)
WINDOW_END = (9, 35)       # 窗口结束 (含), 之后放弃当天买入
CYCLE_SECONDS = 15         # 每股每轮间隔 (秒)
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
    """当前是否处于开盘买入窗口 (周一至周五 09:29-09:35)"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(*WINDOW_START) <= t <= dt_time(*WINDOW_END)


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
        self._stop_flag = False
        self._loop = None
        self._last_error: Optional[str] = None
        self._last_candidates: List[str] = []
        self._candidate_names: Dict[str, str] = {}
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
                "window_start":   f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}",
                "window_end":     f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}",
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
        if not in_buy_window():
            return "[INFO] 当前不在 09:29-09:35 买入窗口, 不启动"
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
            return "[INFO] 弱转强今日无候选, 不启动监控任务"
        with self._lock:
            self._last_candidates = [str(r["stock_code"]) for r in rows]
            self._candidate_names = {str(r["stock_code"]): (r.get("name") or "")
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
        self._save_status()
        return (f"[OK] 开盘买入窗口已启动 -- 已为 {started} 只候选各创建 1 个监控任务, "
                f"每 {CYCLE_SECONDS}s 判定, 触发买入或到 09:35 即结束")

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
        """弱转强 cron 选股池候选 (完整行, 含代码/名称).

        取「最近一批」而非「今天」: 09:26 定时选股时当日日线尚未入库,
        build_universe_snapshot.latest_date = 昨日, 写库 trade_date 是昨天;
        09:29 窗口查询若按今天过滤会查不到候选导致窗口不启动。
        已取消"入选后第 N 个交易日"门槛: 入选即评估, 满足买入条件即下单。
        """
        from lib.selection_store import query_selection_pool
        rows = query_selection_pool(strategy=STRATEGY, trigger="cron") or []
        if not rows:
            return []
        latest = max(str(r.get("trade_date") or "") for r in rows)
        return [r for r in rows if str(r.get("trade_date") or "") == latest]

    def _today_candidates(self) -> List[str]:
        rows = self._today_candidate_rows()
        self._candidate_names = {str(r["stock_code"]): (r.get("name") or "")
                                 for r in rows}
        return [str(r["stock_code"]) for r in rows]

    # ------------------------------------------------------------------
    def _monitor_stock(self, code: str) -> None:
        """单只股票的循环监控任务:
        窗口内每 15 秒评估一次; 触发买入下单(无论成败)或窗口结束(09:35)即结束本任务"""
        from lib.strategy_registry import get_strategy
        meta = get_strategy(STRATEGY)
        try:
            while not self._stop_flag:
                if not in_buy_window():
                    # 09:35 后仍未触发买入 -> 结束该股任务
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
            self._place_order(code, result.get("reason", ""))
            return True

    # ------------------------------------------------------------------
    def _place_order(self, code: str, reason: str) -> dict:
        """下单 (必须在 _order_lock 保护下调用):
        复用 LiveTradingLoop._handle_signal 风控/下单/记账链路, 结果落盘并结束该股任务"""
        s = self._loop.state_store.load()
        sig = {"code": code, "side": "buy", "strategy": STRATEGY, "reason": reason}
        s["signals"] = s.get("signals", [])
        s["orders"] = s.get("orders", [])
        s["signals"].append({**sig, "ts": datetime.now().isoformat(timespec="seconds")})
        log.info("[WINDOW] 买入信号 -> %s (%s)", code, reason)
        order_result = self._loop._handle_signal(s, sig, quantity=100)
        s["orders"].append({**order_result, "ts": datetime.now().isoformat(timespec="seconds")})
        s["signals"] = s["signals"][-100:]
        s["orders"] = s["orders"][-200:]
        self._loop.state_store.save(s)

        with self._lock:
            d = self._stocks.get(code) or {}
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
        """窗口结束(09:35)仍未触发买入 -> 结束该股监控任务"""
        with self._lock:
            d = self._stocks.get(code)
            if d is not None and d.get("task_status") != "ordered":
                d["task_status"] = "window_end"
                d["task_status_text"] = _TASK_STATUS_TEXT["window_end"]
                d["not_buy_reason"] = d.get("not_buy_reason") or "窗口结束(09:35)未触发买入"
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

    def _detail(self, code: str) -> dict:
        """取/建某只候选的判定详情槽位 (锁内调用)"""
        d = self._stocks.get(code)
        if d is None:
            d = {
                "code": code,
                "name": self._candidate_names.get(code, ""),
                "open_price": None,
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
