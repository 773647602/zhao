# -*- coding: utf-8 -*-
# 23-CASE-A: 盘中全自动交易闭环主循环
"""
LiveLoop -- 盘中全自动交易闭环主循环

每隔 N 分钟跑一遍, 完成: 拉行情 -> 评估持仓 -> 跑信号 -> 风控审批 -> 下单 -> 推送

核心架构 (LangGraph 风格, 但简化为顺序循环, 因为每分钟级延迟比 LangGraph 启动开销重要):

    每分钟循环:
        1. health_check()          检查 miniQMT 连接 + 行情数据完整性
        2. update_positions()      拉最新持仓 + 当日盈亏
        3. check_circuit_breaker() 当日亏损是否触发熔断
        4. evaluate_stop_loss()    持仓股是否触发止损
        5. evaluate_signals()      候选股是否出现新信号
        6. risk_check()            风控审批 (Kris 规则)
        7. place_orders()          下单 (本 CASE live_trading.miniqmt_trader_v2)
        8. push_summary()          推送告警 (alert_router)
        9. save_state()            落盘 state (供 CEO 控制台读)

异常处理金字塔:
    L1 数据层异常 -> 跳过本轮, 下轮继续, 不告警 (网络抖动)
    L2 风控否决   -> 不下单, INFO 推送
    L3 订单失败   -> WARN 推送, 重试 1 次
    L4 系统级异常 -> CRITICAL 推送 + 暂停所有交易 (state.trading_status = "HALTED")
    L5 不可恢复   -> FATAL 推送 + 进程退出 + 等人工

注意:
    - 真正的实盘需要接 miniQMT, 在 dry-run 下用模拟数据 (适合教学/演示)
    - 信号评估这里可用 MACD/RSI 占位，实战可替换为自有选股 / 路由输出。
"""

from __future__ import annotations
import json
import math
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alerting.alert_router import AlertRouter
from live_trading.state_store import StateStore


# ============================================================
# 数据来源 (dry-run 下用 xtdata + 模拟下单)
# ============================================================

class MarketDataProvider:
    """市场数据提供者 -- 优先 BigQMT 桥 (国金大QMT, ZMQ 15615), 退回 文件/socket 桥 -> 免费公网 -> xtdata"""

    def __init__(self):
        self._connected = False
        self._bigqmt = None  # 缓存 BigQMT 客户端 (BigQmtXtData); 不可用时保留 None + 下次重探
        self._bigqmt_next_probe = 0.0
        self._bridge = None  # 缓存可用的桥模块; 不可用时保留 False + 下次重探时间
        self._next_probe = 0.0

    def _bigqmt_provider(self):
        """懒加载 BigQMT 桥客户端。探测节流 30s。返回 None 表示当前不可用。"""
        import time as _t
        if self._bigqmt is not None:
            return self._bigqmt
        if _t.time() < self._bigqmt_next_probe:
            return None
        self._bigqmt_next_probe = _t.time() + 30.0
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "config"))
            sys.path.insert(0, str(PROJECT_ROOT / "third_party" / "xtquant_big_convert" / "src"))
            from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient, BigQmtXtData
            client = BigQmtRpcClient()
            client.call("ping", timeout_seconds=5.0)
            self._bigqmt = BigQmtXtData(client)
            return self._bigqmt
        except Exception:
            self._bigqmt = None
            return None

    def _bridge_provider(self):
        import time as _t
        if self._bridge:
            return self._bridge
        # 探测节流: 不可用时每 30s 重试一次 (QMT 桥可能稍后才起来)
        if _t.time() < self._next_probe:
            return None
        self._next_probe = _t.time() + 30.0
        for mod_name in ("lib.qmt_file_bridge", "lib.qmt_socket_bridge"):
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                if mod.bridge_available():
                    self._bridge = mod
                    return mod
            except Exception:
                continue
        self._bridge = None
        return None

    def connect(self):
        if self._bigqmt_provider():
            self._connected = True
            return
        if self._bridge_provider():
            self._connected = True
            return
        from xtquant import xtdata
        xtdata.connect()
        self._connected = True

    def get_latest_tick(self, stock_code: str) -> dict:
        """拉最新 tick: BigQMT 桥 -> 免费公网 (腾讯) -> xtdata"""
        bigqmt = self._bigqmt_provider()
        if bigqmt:
            try:
                ticks = bigqmt.get_full_tick([stock_code])
                t = ticks.get(stock_code)
                if t:
                    return t
            except Exception:
                pass
        bridge = self._bridge_provider()
        if bridge:
            try:
                ticks = bridge.get_full_tick([stock_code])
                return ticks.get(stock_code, {})
            except Exception:
                pass
        try:
            from lib import free_market
            ticks = free_market.get_latest_ticks([stock_code])
            if ticks.get(stock_code):
                return ticks[stock_code]
        except Exception:
            pass
        from xtquant import xtdata
        if not self._connected:
            self.connect()
        ticks = xtdata.get_full_tick([stock_code])
        return ticks.get(stock_code, {})

    def get_recent_kline(self, stock_code: str, period: str = "5m",
                        count: int = 50) -> Optional[Any]:
        """拉最近 N 根 K 线 (用于算指标)"""
        import pandas as pd
        bigqmt = self._bigqmt_provider()
        if bigqmt:
            try:
                data = bigqmt.get_market_data_ex(
                    field_list=["open", "high", "low", "close", "volume"],
                    stock_list=[stock_code], period=period, count=count,
                )
                df = data.get(stock_code)
                if df is not None and len(df) > 0:
                    df = df.copy()
                    df.index = pd.to_datetime(df.index)
                    return df
            except Exception:
                pass
        bridge = self._bridge_provider()
        if bridge:
            try:
                data = bridge.get_market_data_ex(
                    field_list=["open", "high", "low", "close", "volume"],
                    stock_list=[stock_code], period=period, count=count,
                )
                df = data.get(stock_code)
                if df is not None and len(df) > 0:
                    df = df.copy()
                    df.index = pd.to_datetime(df.index)
                    return df
            except Exception:
                pass
        # 免费公网分钟K (新浪)
        try:
            from lib import free_market
            df = free_market.load_intraday_kline(stock_code, period=period)
            if df is not None and len(df):
                return df.tail(count)
        except Exception:
            pass
        from xtquant import xtdata
        if not self._connected:
            self.connect()
        try:
            xtdata.download_history_data(stock_code, period=period,
                                         start_time="20250101", incrementally=True)
            data = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume"],
                stock_list=[stock_code], period=period, count=count,
            )
            df = data.get(stock_code)
            if df is None or len(df) == 0:
                return None
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            return df
        except Exception:
            return None


# ============================================================
# 信号评估 (简化版 MACD)
# ============================================================

def evaluate_macd_signal(df) -> str:
    """
    评估 MACD 信号
    返回: "buy" / "sell" / "hold"
    """
    import pandas as pd
    if df is None or len(df) < 30:
        return "hold"
    close = df["close"].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    # 最新两根: 看是否金叉/死叉
    if len(dif) < 2:
        return "hold"
    prev = dif.iloc[-2] - dea.iloc[-2]
    curr = dif.iloc[-1] - dea.iloc[-1]
    if prev <= 0 and curr > 0:
        return "buy"
    if prev >= 0 and curr < 0:
        return "sell"
    return "hold"


# ============================================================
# 持仓与盈亏更新
# ============================================================

def update_positions_from_market(positions: List[dict],
                                 market: MarketDataProvider) -> List[dict]:
    """拉最新价更新持仓的市值 + 浮动盈亏"""
    updated = []
    for pos in positions:
        code = pos["code"]
        tick = market.get_latest_tick(code)
        cur_price = float(tick.get("lastPrice", pos.get("cost", 0)))
        volume = int(pos["volume"])
        cost = float(pos.get("cost", 0))
        mv = volume * cur_price
        pnl = (cur_price - cost) * volume
        pnl_pct = (cur_price - cost) / cost if cost > 0 else 0
        updated.append({
            **pos,
            "cur_price":   round(cur_price, 3),
            "market_value": round(mv, 2),
            "pnl":         round(pnl, 2),
            "pnl_pct":     round(pnl_pct, 4),
        })
    return updated


def calc_today_pnl(positions: List[dict], capital: float) -> tuple:
    """计算当日总盈亏 (元 + 百分比)"""
    total_pnl = sum(p.get("pnl", 0) for p in positions)
    total_pct = total_pnl / capital if capital > 0 else 0
    return round(total_pnl, 2), round(total_pct, 4)


def apply_fill(state: dict, code: str, side: str, quantity: int,
               price: float, name: str = "") -> None:
    """把一笔成交回填到内存持仓 (state.positions).

    买入: 新增或摊薄成本加大持仓; 卖出: 减仓, 减到 0 移除.
    使「实时持仓」在模拟(dry-run)与实盘(submitted)成交后都同步更新。
    """
    positions = state.setdefault("positions", [])
    qty = int(quantity or 0)
    price = float(price or 0)
    for p in positions:
        if p.get("code") == code:
            vol = int(p.get("volume", 0))
            if side == "buy":
                new_vol = vol + qty
                cost = ((vol * float(p.get("cost", 0)) + qty * price) / new_vol
                        if new_vol else price)
                p["volume"] = new_vol
                p["cost"] = round(cost, 4)
                if name:
                    p["name"] = name
            else:
                p["volume"] = max(0, vol - qty)
            if int(p.get("volume", 0)) <= 0:
                positions.remove(p)
            return
    if side == "buy":
        positions.append({
            "code": code, "name": name, "volume": qty,
            "cost": round(price, 4), "cur_price": price,
            "market_value": round(price * qty, 2), "pnl": 0.0, "pnl_pct": 0.0,
        })


# ============================================================
# 主循环
# ============================================================

class LiveTradingLoop:
    """
    实盘主循环 (默认 dry-run)

    用法:
        loop = LiveTradingLoop(watch_stocks=["600519.SH", "513100.SH"])
        loop.run_once()         # 跑一次
        loop.run_forever(60)    # 每 60 秒跑一次, 直到 Ctrl+C
    """

    def __init__(self,
                 watch_stocks: List[str],
                 capital: float = 1_000_000,
                 state_file: str = "outputs/live_state.json",
                 max_daily_loss_pct: float = -0.02,
                 dry_run: bool = False,   # 恒为实盘: 不再有模拟撮合分支
                 signal_evaluator: Optional[Callable[[str, "MarketDataProvider", float], dict]] = None,
                 per_strategy_mode: bool = True):
        """
        signal_evaluator: 可选的传统信号评估器, 签名 (code, market, capital) -> dict
            返回字典 {"side": "buy"/"sell"/"hold", "strategy": str, "reason": str (可选)}
            per_strategy_mode=True 时忽略 signal_evaluator, 改为按策略独立评估:
            每个启用策略只评估它自己的选股池 + 持仓, 信号带策略标签, 互不影响 (无 default 兜底)
        """
        self.watch_stocks = watch_stocks
        self.capital = capital
        self.dry_run = dry_run
        self.max_daily_loss_pct = max_daily_loss_pct
        self.signal_evaluator = signal_evaluator
        self.per_strategy_mode = per_strategy_mode

        self.state_store = StateStore(state_file)
        self.market = MarketDataProvider()
        self.alert = AlertRouter(info_aggregate_seconds=300)
        # 真实交易用: trader 单例复用 + 心跳 + 自动重连 (实测可下单选走 BigQMT 桥)
        self._trader = None
        self._trader_lock = threading.Lock()

        # 初始化 state
        s = self.state_store.load()
        s["capital"] = capital
        s["watch_stocks"] = watch_stocks
        s["control"]["dry_run"] = dry_run
        s["control"]["max_daily_loss"] = max_daily_loss_pct
        self.state_store.save(s)

    # ------------------------------------------------------------------
    # 单次循环
    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        """跑一次完整循环"""
        cycle_start = time.time()
        s = self.state_store.load()

        # 0) 检查 control.trading_status
        if s.get("trading_status") == "HALTED":
            self.alert.alert("WARN", "交易已熔断, 跳过本轮", source="loop")
            return {"action": "halted_skip"}
        if s.get("trading_status") == "PAUSED":
            self.alert.alert("INFO", "交易已暂停 (CEO 控制台暂停)", source="loop")
            return {"action": "paused_skip"}

        # 1) health_check
        try:
            self.market.connect()
            s["health"]["miniqmt_connected"] = True
            s["health"]["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            s["health"]["miniqmt_connected"] = False
            s["health"]["errors_24h"] = s["health"].get("errors_24h", 0) + 1
            self.alert.alert("CRITICAL", "miniQMT 连接失败",
                             message=str(e), source="health")
            self.state_store.save(s)
            return {"action": "health_fail"}

        # 2) 更新持仓 + 当日盈亏 -- 以国金真实持仓为准 (对接券商记录)
        positions = self.sync_positions_from_broker(s)
        if positions is None:
            # 券商查询失败: 保留内存持仓做展示, 但不据此下单
            positions = s.get("positions", [])
            if positions:
                positions = update_positions_from_market(positions, self.market)
        if positions:
            today_pnl, today_pnl_pct = calc_today_pnl(positions, self.capital)
            s["positions"] = positions
            s["today_pnl"] = today_pnl
            s["today_pnl_pct"] = today_pnl_pct
            s["pnl_history"] = s.get("pnl_history", [])
            s["pnl_history"].append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "pnl": today_pnl, "pnl_pct": today_pnl_pct,
            })
            s["pnl_history"] = s["pnl_history"][-500:]
        else:
            s["positions"] = []

        # 3) 熔断检查
        if s.get("today_pnl_pct", 0) <= self.max_daily_loss_pct:
            s["trading_status"] = "HALTED"
            self.alert.alert(
                "CRITICAL", "触发当日亏损熔断",
                message=f"今日累计盈亏 {s['today_pnl_pct']:.2%}, "
                        f"已跌破熔断线 {self.max_daily_loss_pct:.2%}",
                source="circuit_breaker",
            )
            self.state_store.save(s)
            return {"action": "circuit_breaker"}

        # 4) 评估信号 (新模型: 每个启用策略只评估自己的选股池+持仓, 互不影响, 无 default 兜底)
        new_signals = []
        if self.per_strategy_mode:
            from lib.selection_engine import iter_strategy_signal_targets
            from lib.strategy_registry import get_strategy
            for sname, code in iter_strategy_signal_targets():
                meta = get_strategy(sname)
                if meta is None:
                    continue
                try:
                    result = meta.evaluator(code, self.market, self.capital)
                except Exception as e:
                    self.alert.alert("WARN", f"{sname} 策略评估异常 {code}",
                                     message=str(e), source="zoe")
                    continue
                if not result:
                    continue
                side = result.get("side", "hold")
                if side == "hold":
                    continue
                new_signals.append({
                    "code": code, "side": side,
                    "strategy": sname, "reason": result.get("reason", ""),
                })
        else:
            for code in self.watch_stocks:
                if self.signal_evaluator is not None:
                    # 传统外部注入的信号路由器: 由 evaluator 自己决定用哪个策略
                    try:
                        result = self.signal_evaluator(code, self.market, self.capital)
                    except Exception as e:
                        self.alert.alert("WARN", f"signal_evaluator 异常 {code}",
                                         message=str(e), source="zoe")
                        continue
                    if not result:
                        continue
                    side = result.get("side", "hold")
                    if side == "hold":
                        continue
                    sig = {
                        "code":     code,
                        "side":     side,
                        "strategy": result.get("strategy", "unknown"),
                        "reason":   result.get("reason", ""),
                    }
                else:
                    df = self.market.get_recent_kline(code, period="5m", count=50)
                    side = evaluate_macd_signal(df)
                    if side == "hold":
                        continue
                    sig = {"code": code, "side": side, "strategy": "macd_5min"}
                new_signals.append(sig)

        # 注意: signals / orders / events 都先攒在内存 s 里, 第 6 步统一一次 save 落盘
        # -- 不能用 append_signal / append_order, 否则它们 load->改->save, 会被第 6 步的
        # save(s) 用旧快照整体覆盖回去, 导致 signals / orders 写完即丢
        if new_signals:
            s["signals"] = s.get("signals", [])
            for sig in new_signals:
                s["signals"].append({
                    **sig,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                self.alert.alert(
                    "INFO", f"信号触发 -> {sig['side']} {sig['code']} [{sig.get('strategy','')}]",
                    source="zoe",
                )
            s["signals"] = s["signals"][-100:]

        # 5) 风控 + 下单
        s["orders"] = s.get("orders", [])
        for sig in new_signals:
            order_result = self._handle_signal(s, sig)
            # 把触发该订单的策略名一起记录, 便于复盘
            if "strategy" not in order_result and sig.get("strategy"):
                order_result["strategy"] = sig["strategy"]
            s["orders"].append({
                **order_result,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
        s["orders"] = s["orders"][-100:]

        # 6) 落盘 state (本轮所有改动: positions/today_pnl/pnl_history/health/signals/orders/events 一锅端)
        s["events"] = s.get("events", [])
        s["events"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": "loop_cycle",
            "signal_count": len(new_signals),
            "duration_ms": int((time.time() - cycle_start) * 1000),
        })
        s["events"] = s["events"][-200:]
        self.state_store.save(s)

        return {
            "action":      "cycle_done",
            "duration_ms": int((time.time() - cycle_start) * 1000),
            "new_signals": len(new_signals),
        }

    def _get_trader(self):
        """返回真实交易账号的单例 trader (BigQMT 桥, 心跳+自动重连)。首次调用才连接。"""
        with self._trader_lock:
            if self._trader is not None:
                return self._trader
            from live_trading.miniqmt_trader_v2 import MiniQMTTraderV2
            trader = MiniQMTTraderV2(
                qmt_path=os.environ.get("QMT_PATH", ""),
                account_id=os.environ["ACCOUNT_ID"],
                enable_heartbeat=True,      # 心跳保活
                heartbeat_interval=10,      # 10s 心跳 (合规项目记忆约束)
                enable_reconnect=True,      # 断线自动重连
                max_reconnect_attempts=5,
            )
            trader.connect()
            self._trader = trader
            return trader

    def _handle_signal(self, state: dict, signal: dict,
                       quantity: Optional[int] = None) -> dict:
        """处理一个信号: 风控 -> 下单 -> 推送

        quantity: 指定买入数量时固定按此手数下单 (弱转强开盘窗口缺省 100 股);
                  为 None 时按"单笔不超过总资金 10%"计算。
        """
        code = signal["code"]
        side = signal["side"]
        tick = self.market.get_latest_tick(code)
        if side == "buy":
            # 买单: 挂买一价 (盘口五档第 1 档, 最高买价, 优先成交);
            # 买一价拿不到时回退最新价
            try:
                bids = tick.get("bidPrice") or []
                if bids and bids[0]:
                    price = float(bids[0])
                else:
                    price = float(tick.get("lastPrice", 0))
            except Exception:
                price = float(tick.get("lastPrice", 0))
        else:
            # 卖单: 按最新价挂限价单
            price = float(tick.get("lastPrice", 0))
        if price <= 0:
            return {"code": code, "side": side, "status": "rejected",
                    "reason": "拿不到价格", "ts": datetime.now().isoformat()}

        # 成交回填到持仓时用的股票名称 (信号里没有则从名称映射取)
        name = signal.get("name") or ""
        if not name:
            try:
                from lib.selection_engine import _stock_name_map
                name = _stock_name_map().get(code, "")
            except Exception:
                name = ""

        # 数量: 弱转强窗口缺省 100 股; 其余信号按 10% 资金风控计算
        if quantity is not None:
            quantity = max(100, int(quantity) // 100 * 100)
        else:
            max_amount = self.capital * 0.10
            quantity = int(max_amount / price / 100) * 100
            if quantity == 0:
                quantity = 100   # 至少 1 手试探

        amount = quantity * price

        # 共享资金/独立持仓: 买入前校验各策略聚合投入成本不超过总资金
        if side == "buy":
            try:
                from lib.selection_store import query_strategy_positions
                invested = sum(
                    (p.get("volume", 0) or 0) * (p.get("cost", 0) or 0)
                    for p in query_strategy_positions()
                )
                if invested + amount > self.capital:
                    self.alert.alert("WARN", f"聚合资金不足, 拒绝买入 {code}",
                                     source="risk")
                    return {"code": code, "side": side, "strategy": signal.get("strategy", "unknown"),
                            "status": "rejected", "reason": "共享总资金不足", "ts": datetime.now().isoformat()}
            except Exception:
                pass

        # control.pause_buying 拦截
        if side == "buy" and state.get("control", {}).get("pause_buying"):
            self.alert.alert("INFO", "买入被 CEO 控制台暂停",
                             message=f"{code} {quantity}股 @ {price:.2f}",
                             source="control")
            return {"code": code, "side": side, "quantity": quantity,
                    "price": price, "status": "paused_by_ceo"}

        # 真实下单 (BigQMT 桥单例 trader, 心跳+自动重连; 复用已连接实例避免重复开销; 恒为实盘, 无模拟撮合)
        try:
            trader = self._get_trader()   # 首次调用才 connect, 后续复用
            if side == "buy":
                order_id = trader.buy(code, quantity, price=price,
                                      strategy_name="live_loop")
            else:
                order_id = trader.sell(code, quantity, price=price,
                                       strategy_name="live_loop")

            if order_id:
                self.alert.alert(
                    "INFO", f"实盘下单成功 {side} {code}",
                    message=f"委托编号 {order_id}, {quantity}股 @ {price:.2f}",
                    source="trader",
                )
                # 独立持仓记账: 内存持仓 + 按策略落库 trade_strategy_position
                strategy = signal.get("strategy", "unknown")
                apply_fill(state, code, side, quantity, price, name)
                try:
                    from lib.selection_store import increment_position
                    increment_position(strategy, code, side, quantity, price)
                except Exception:
                    pass
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "amount": amount, "status": "submitted",
                        "order_id": order_id, "strategy": strategy,
                        "ts": datetime.now().isoformat()}
            else:
                self.alert.alert("WARN", f"实盘下单失败 {code}", source="trader")
                return {"code": code, "side": side, "quantity": quantity,
                        "price": price, "status": "failed",
                        "ts": datetime.now().isoformat()}
        except Exception as e:
            self.alert.alert("CRITICAL", f"下单异常 {code}",
                             message=str(e), source="trader")
            return {"code": code, "side": side, "status": "exception",
                    "reason": str(e), "ts": datetime.now().isoformat()}

    def sync_positions_from_broker(self, state: dict) -> Optional[List[dict]]:
        """对接国金证券真实持仓: 用 miniQMT 查询真实持仓覆盖 state.positions.

        返回持仓列表 (含 cur_price/market_value/pnl/pnl_pct); 查询失败返回 None (调用方回退内存持仓)。
        """
        try:
            trader = self._get_trader()
        except Exception as e:
            self.alert.alert("WARN", "连接交易账号失败, 持仓同步跳过",
                             message=str(e), source="trader")
            return None
        try:
            # 查询国金真实持仓
            raw = trader.query_positions()
            if raw is None:
                return None
            # 查询国金真实资产 (总资产/现金/市值); 仅当返回有效正资产才覆盖, 避免休市返回 0 误覆盖
            asset = trader.query_asset()
            if asset:
                state["asset"] = asset
                if float(asset.get("total_asset", 0) or 0) > 0:
                    state["capital"] = asset["total_asset"]

            positions = []
            for p in raw:
                code = p.get("stock_code", "")
                volume = int(p.get("volume", 0) or 0)
                if volume <= 0:
                    continue
                cost = float(p.get("open_price", 0) or 0)   # 持仓成本价
                tick = self.market.get_latest_tick(code)
                cur_price = float(tick.get("lastPrice", cost) or cost)
                mv = volume * cur_price
                pnl = (cur_price - cost) * volume
                pnl_pct = (cur_price - cost) / cost if cost > 0 else 0
                positions.append({
                    "code": code,
                    "name": self._stock_name(code),
                    "volume": volume,
                    "cost": round(cost, 4),
                    "cur_price": round(cur_price, 3),
                    "market_value": round(mv, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                })
            return positions
        except Exception as e:
            self.alert.alert("WARN", "券商持仓查询失败, 使用内存持仓展示",
                             message=str(e), source="trader")
            return None

    @staticmethod
    def _stock_name(code: str) -> str:
        """解析股票名称: 优先引擎内部名称映射, 缺省用代码"""
        try:
            from lib.selection_engine import _stock_name_map
            return _stock_name_map().get(code, code)
        except Exception:
            return code

    # ------------------------------------------------------------------
    # 长跑模式
    # ------------------------------------------------------------------
    def run_forever(self, interval_seconds: int = 60):
        """每隔 N 秒跑一次, 直到 Ctrl+C"""
        self.alert.alert("INFO", "实盘主循环启动",
                         message=f"watch={self.watch_stocks}, "
                                 f"interval={interval_seconds}s, dry_run={self.dry_run}",
                         source="loop")
        try:
            while True:
                t0 = time.time()
                result = self.run_once()
                # 等到下一次触发
                elapsed = time.time() - t0
                if elapsed < interval_seconds:
                    time.sleep(interval_seconds - elapsed)
        except KeyboardInterrupt:
            self.alert.alert("INFO", "实盘主循环退出 (Ctrl+C)", source="loop")
            self.alert.shutdown()
            self._shutdown_trader()

    def _shutdown_trader(self):
        """优雅断开真实交易 trader 单例 (若有)"""
        with self._trader_lock:
            if self._trader is not None:
                try:
                    self._trader.disconnect()
                except Exception:
                    pass
                self._trader = None


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    # CLI 独立进程: 加载 .env, 使 MySQL/BigQMT/桥 等配置可用 (与 app.py 一致)
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="盘中全自动交易闭环")
    parser.add_argument("--stocks", default="600519.SH,513100.SH",
                        help="监控股票池, 逗号分隔")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--interval", type=int, default=60,
                        help="循环间隔秒, 默认 60")
    parser.add_argument("--once", action="store_true", help="只跑一次")
    parser.add_argument("--state-file", default="outputs/live_state.json")
    args = parser.parse_args()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    loop = LiveTradingLoop(
        watch_stocks=stocks,
        capital=args.capital,
        state_file=args.state_file,
        dry_run=False,   # CLI 独立进程仍走实盘
    )

    if args.once:
        result = loop.run_once()
        print(f"\n[完成] {result}")
        print(f"\nstate 落盘: {args.state_file}")
    else:
        loop.run_forever(args.interval)


if __name__ == "__main__":
    main()
