# -*- coding: utf-8 -*-
# 弱转强开盘买入窗口 单元测试 (unittest 标准库, 零第三方依赖)
"""
覆盖范围:
1. open_buy_window.in_buy_window            -- 时间窗口边界 / 工作日判定
2. OpenBuyWindowRunner                      -- 候选读取 / _run_cycle 各分支 / start/stop
3. strat_weak_to_strong 买入时间窗口         -- 窗口外放弃买入 / 窗口内可买 / 持仓卖出不受限

运行方式 (项目根目录):
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime as _real_datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import datetime as _dt_mod
import pandas as pd

from live_trading.open_buy_window import OpenBuyWindowRunner, in_buy_window, WINDOW_START, WINDOW_END


# ------------------------------------------------------------------
# 工具: 固定时间的 FakeDatetime (patch datetime.datetime 用)
# ------------------------------------------------------------------
class FakeDatetime(_real_datetime):
    """now() 返回固定时间; 其余行为继承真实 datetime"""

    _fixed: _real_datetime = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def freeze_time(y, mo, d, h, mi, s=0):
    """返回固定当前时间的 patch 上下文。

    同时 patch 两处:
      - "datetime.datetime" (函数体内 from datetime import datetime 的路径)
      - "live_trading.open_buy_window.datetime" (模块顶层绑定的引用)
    """
    FakeDatetime._fixed = _real_datetime(y, mo, d, h, mi, s)
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(mock.patch("datetime.datetime", FakeDatetime))
    stack.enter_context(mock.patch("live_trading.open_buy_window.datetime",
                                   FakeDatetime))
    return stack


# 2026-09-09 是周三 (weekday=2, 工作日); 用它的 09:30 作为窗口内时间
INSIDE = (2026, 9, 9, 9, 30)
OUTSIDE_AFTER = (2026, 9, 9, 9, 36)     # 窗口刚结束
OUTSIDE_EARLY = (2026, 9, 9, 9, 28)     # 窗口开始前
WEEKEND_INSIDE = (2026, 9, 12, 9, 30)   # 2026-09-12 周六, 即使窗口内也不算


# ------------------------------------------------------------------
# 1) in_buy_window 时间边界
# ------------------------------------------------------------------
class TestInBuyWindow(unittest.TestCase):

    def _check(self, y, mo, d, h, mi, s=0):
        t = _real_datetime(y, mo, d, h, mi, s)
        return in_buy_window(t)

    def test_window_start_inclusive(self):
        self.assertTrue(self._check(*INSIDE[:3], *WINDOW_START, 0))

    def test_window_end_inclusive(self):
        self.assertTrue(self._check(*INSIDE[:3], *WINDOW_END, 0))

    def test_inside(self):
        self.assertTrue(self._check(*INSIDE))

    def test_early_outside(self):
        self.assertFalse(self._check(*OUTSIDE_EARLY))

    def test_after_outside(self):
        self.assertFalse(self._check(*OUTSIDE_AFTER))

    def test_weekend_inside_window_is_false(self):
        self.assertFalse(self._check(*WEEKEND_INSIDE))


# ------------------------------------------------------------------
# 2) OpenBuyWindowRunner
# ------------------------------------------------------------------
class FakeStateStore:
    """模拟 StateStore.load / save"""

    def __init__(self, state=None):
        self._state = state if state is not None else {
            "signals": [], "orders": [], "trading_status": "RUNNING",
        }
        self.save_calls = 0

    def load(self):
        return dict(self._state)

    def save(self, state):
        self.save_calls += 1
        self._state = state


class FakeLoop:
    """模拟 LiveTradingLoop 的最小对象 (_evaluate_and_order 只用到这些)"""

    def __init__(self, state=None, order_status="submitted"):
        self.state_store = FakeStateStore(state)
        self.market = mock.Mock()
        self.capital = 1_000_000.0
        self.handled: list = []
        self.order_status = order_status

    def _handle_signal(self, state, signal, quantity=None):
        self.handled.append(signal)
        return {"code": signal["code"], "side": "buy",
                "status": self.order_status,
                "strategy": signal.get("strategy", ""),
                "reason": "" if self.order_status == "submitted" else "下单失败",
                "quantity": quantity or 100, "price": 10.0, "amount": 1000.0}


def fresh_runner() -> OpenBuyWindowRunner:
    """重置单例并返回一个全新实例 (隔离测试间状态)"""
    OpenBuyWindowRunner._instance = None
    return OpenBuyWindowRunner()


class TestTodayCandidates(unittest.TestCase):

    def test_extracts_stock_codes(self):
        rows = [
            {"stock_code": "600519.SH", "trade_date": "2026-09-09", "source_type": "cron"},
            {"stock_code": "000001.SZ", "trade_date": "2026-09-09", "source_type": "cron"},
        ]
        runner = fresh_runner()
        with mock.patch("lib.selection_store.query_selection_pool", return_value=rows), \
                freeze_time(*INSIDE):
            self.assertEqual(runner._today_candidates(),
                             ["600519.SH", "000001.SZ"])


class TestEvaluateAndOrder(unittest.TestCase):
    """单股单轮评估 (每股监控任务的核心):
    触发买入 -> 下单 -> 无论下单成败任务结束; 未触发则保持监控"""

    def _runner(self, state=None, order_status="submitted"):
        runner = fresh_runner()
        runner._loop = FakeLoop(state, order_status=order_status)
        runner._candidate_names = {"600519.SH": "贵州茅台"}
        return runner

    def test_all_hold_no_order_task_keeps_monitoring(self):
        runner = self._runner()
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "hold", "reason": "条件未满足"}
        self.assertFalse(runner._evaluate_and_order("600519.SH", meta))
        self.assertEqual(runner._loop.handled, [])
        self.assertEqual(runner._loop.state_store.save_calls, 0)
        d = runner._stocks["600519.SH"]
        self.assertEqual(d["task_status"], "monitoring")
        self.assertEqual(d["not_buy_reason"], "条件未满足")
        self.assertEqual(d["cycles"], 1)

    def test_buy_signal_places_order_and_ends_task(self):
        runner = self._runner()
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "buy", "reason": "弱转强买入(开盘买)"}
        self.assertTrue(runner._evaluate_and_order("600519.SH", meta))
        # 下单 1 次, 状态落盘 1 次
        self.assertEqual([s["code"] for s in runner._loop.handled], ["600519.SH"])
        self.assertEqual(runner._loop.state_store.save_calls, 1)
        # 任务结束: 已下单
        d = runner._stocks["600519.SH"]
        self.assertEqual(d["task_status"], "ordered")
        self.assertEqual(d["order_status"], "submitted")
        self.assertEqual(d["bought"], 1)
        self.assertEqual(d["buy_reason"], "弱转强买入(开盘买)")
        saved = runner._loop.state_store._state
        self.assertEqual(len(saved["signals"]), 1)
        self.assertEqual(len(saved["orders"]), 1)

    def test_order_failed_still_ends_task(self):
        """下单失败(如风控/资金不足) -> 任务同样结束, 不再重复监控"""
        runner = self._runner(order_status="failed")
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "buy", "reason": "弱转强买入(开盘买)"}
        self.assertTrue(runner._evaluate_and_order("600519.SH", meta))
        self.assertEqual(len(runner._loop.handled), 1)
        d = runner._stocks["600519.SH"]
        self.assertEqual(d["task_status"], "ordered")
        self.assertEqual(d["order_status"], "failed")
        self.assertEqual(d["bought"], 0)

    def test_halted_skips_orders_task_keeps_monitoring(self):
        runner = self._runner(state={"signals": [], "orders": [],
                                     "trading_status": "HALTED"})
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "buy", "reason": "弱转强买入"}
        self.assertFalse(runner._evaluate_and_order("600519.SH", meta))
        self.assertEqual(runner._loop.handled, [])
        self.assertEqual(runner._loop.state_store.save_calls, 0)
        self.assertIn("暂停买入", runner._stocks["600519.SH"]["not_buy_reason"])

    def test_evaluator_exception_returns_false(self):
        runner = self._runner()
        meta = mock.Mock()
        meta.evaluator.side_effect = RuntimeError("行情异常")
        self.assertFalse(runner._evaluate_and_order("600519.SH", meta))
        self.assertEqual(runner._loop.handled, [])
        self.assertIn("评估异常", runner._stocks["600519.SH"]["not_buy_reason"])

    def test_already_ordered_not_re_evaluated(self):
        """任务已结束(ordered)后再调用 -> 直接结束, 不重复评估/下单"""
        runner = self._runner()
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "buy", "reason": "弱转强买入(开盘买)"}
        self.assertTrue(runner._evaluate_and_order("600519.SH", meta))
        self.assertTrue(runner._evaluate_and_order("600519.SH", meta))
        self.assertEqual(len(runner._loop.handled), 1)
        self.assertEqual(runner._loop.state_store.save_calls, 1)


class TestStartStop(unittest.TestCase):

    def test_start_outside_window_refused(self):
        runner = fresh_runner()
        with freeze_time(*OUTSIDE_AFTER):
            msg = runner.start()
        self.assertIn("不启动", msg)
        self.assertEqual(runner._threads, {})

    def test_start_no_candidates(self):
        runner = fresh_runner()
        with mock.patch("live_trading.live_loop.LiveTradingLoop",
                        return_value=FakeLoop()), \
                mock.patch.object(runner, "_today_candidate_rows", return_value=[]), \
                mock.patch("lib.live_simulator.load_mock_config",
                           return_value={"capital": 1_000_000, "positions": []}), \
                mock.patch("lib.paths.OUTPUTS_LIVE_STATE",
                           Path(PROJECT_ROOT) / "outputs" / "live_state.json"), \
                freeze_time(*INSIDE):
            msg = runner.start()
        self.assertIn("无候选", msg)
        self.assertFalse(runner.status().get("running"))
        self.assertEqual(runner._threads, {})

    def test_start_inside_window_spawns_per_stock_threads(self):
        """每股一个独立监控任务 (线程): 2 只候选 -> 2 个线程"""
        runner = fresh_runner()
        rows = [
            {"stock_code": "600519.SH", "trade_date": "2026-09-09",
             "name": "贵州茅台", "source_type": "cron"},
            {"stock_code": "000001.SZ", "trade_date": "2026-09-09",
             "name": "平安银行", "source_type": "cron"},
        ]
        meta = mock.Mock()
        meta.evaluator.return_value = {"side": "hold", "reason": "条件未满足"}
        with mock.patch("live_trading.live_loop.LiveTradingLoop",
                        return_value=FakeLoop()), \
                mock.patch.object(runner, "_today_candidate_rows", return_value=rows), \
                mock.patch("lib.live_simulator.load_mock_config",
                           return_value={"capital": 1_000_000, "positions": []}), \
                mock.patch("lib.paths.OUTPUTS_LIVE_STATE",
                           Path(PROJECT_ROOT) / "outputs" / "live_state.json"), \
                mock.patch("lib.strategy_registry.get_strategy", return_value=meta), \
                freeze_time(*INSIDE):
            msg = runner.start()
        self.assertIn("[OK]", msg)
        self.assertIn("2 只候选", msg)
        self.assertEqual(set(runner._threads.keys()),
                         {"600519.SH", "000001.SZ"})
        self.assertTrue(runner.status().get("running"))
        self.assertTrue(all(t.is_alive() for t in runner._threads.values()))
        msg = runner.stop()
        self.assertIn("[OK]", msg)
        self.assertFalse(runner.status().get("running"))

    def test_stop_not_running(self):
        runner = fresh_runner()
        self.assertIn("未运行", runner.stop())


# ------------------------------------------------------------------
# 3) strat_weak_to_strong 买入时间窗口
# ------------------------------------------------------------------
class FakeMarket:
    """模拟行情提供者: get_recent_kline 返回构造好的日K, get_latest_tick 无 tick"""

    def __init__(self, df=None, mdf=None, tick=None):
        self._df = df
        self._mdf = mdf
        self._tick = tick

    def get_recent_kline(self, code, period="1d", count=40):
        if period == "1d":
            return self._df
        return self._mdf

    def get_latest_tick(self, code):
        return self._tick or {}


def daily_df() -> pd.DataFrame:
    """两天日K: 2026-09-08 收 10.20, 2026-09-09 开 10.50 (开盘涨幅≈2.94%)"""
    idx = pd.to_datetime(["2026-09-08", "2026-09-09"])
    return pd.DataFrame({
        "open": [10.00, 10.50],
        "high": [10.30, 10.90],
        "low": [9.90, 10.40],
        "close": [10.20, 10.80],
    }, index=idx)


def trade_cfg(buy=None, sell=None, enabled=True) -> dict:
    return {"enabled": enabled, "buy": buy or {"days": 0, "mode": "open"},
            "sell": sell or {"take_profit_pct": 10, "stop_loss_pct": -5}}


def patch_store(pool_rows, positions):
    return mock.patch.multiple(
        "lib.selection_store",
        query_selection_pool=mock.Mock(return_value=pool_rows),
        query_strategy_positions=mock.Mock(return_value=positions),
    )


class TestWeakToStrongBuyWindow(unittest.TestCase):

    def _evaluate(self, pool_rows, positions, market, cfg):
        """在给定 mock 环境下调用 strat_weak_to_strong"""
        from lib.strategy_registry import strat_weak_to_strong
        with mock.patch("lib.strategy_registry._weak_trade_config",
                        return_value=cfg), patch_store(pool_rows, positions):
            return strat_weak_to_strong("600519.SH", market, 1_000_000)

    def test_buy_blocked_after_window(self):
        """窗口结束后 (09:36): 无持仓且在选股池 -> 放弃买入"""
        market = FakeMarket(df=daily_df())
        with freeze_time(*OUTSIDE_AFTER):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(),
            )
        self.assertEqual(res.get("side"), "hold")
        self.assertIn("非买入窗口", res.get("reason", ""))

    def test_buy_blocked_before_window(self):
        """窗口开始前 (09:28): 同样放弃买入"""
        market = FakeMarket(df=daily_df())
        with freeze_time(*OUTSIDE_EARLY):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(),
            )
        self.assertEqual(res.get("side"), "hold")
        self.assertIn("非买入窗口", res.get("reason", ""))

    def test_buy_allowed_inside_window(self):
        """窗口内 (09:30): 满足买入条件 -> buy 信号"""
        market = FakeMarket(df=daily_df())
        with freeze_time(*INSIDE):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(),
            )
        self.assertEqual(res.get("side"), "buy")

    def test_no_buy_when_price_rises_above_open(self):
        """开盘向上后不买: 现价 10.60 > 开盘价 10.50 -> hold"""
        market = FakeMarket(df=daily_df(), tick={"lastPrice": 10.60})
        with freeze_time(*INSIDE):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(),
            )
        self.assertEqual(res.get("side"), "hold")
        self.assertIn("开盘向上", res.get("reason", ""))

    def test_buy_allowed_when_price_below_open(self):
        """开盘后回落: 现价 10.40 < 开盘价 10.50 -> 仍可买"""
        market = FakeMarket(df=daily_df(), tick={"lastPrice": 10.40})
        with freeze_time(*INSIDE):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(),
            )
        self.assertEqual(res.get("side"), "buy")

    def test_no_buy_up_pct_threshold(self):
        """可配阈值: no_buy_up_pct=1, 现价 10.60 (+0.95%) 未超阈值 -> 仍可买"""
        market = FakeMarket(df=daily_df(), tick={"lastPrice": 10.60})
        with freeze_time(*INSIDE):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(buy={"mode": "open", "no_buy_up_pct": 1}),
            )
        self.assertEqual(res.get("side"), "buy")

    def test_buy_blocked_if_not_in_pool_inside_window(self):
        """窗口内但不在选股池 -> 不买 (验证窗口检查在选股池检查之前)"""
        market = FakeMarket(df=daily_df())
        with freeze_time(*INSIDE):
            res = self._evaluate(pool_rows=[], positions=[],
                                 market=market, cfg=trade_cfg())
        self.assertEqual(res.get("side"), "hold")

    def test_sell_not_blocked_outside_window(self):
        """持仓卖出不受窗口限制: 窗口外有持仓 -> 不返回'非买入窗口'"""
        positions = [{"stock_code": "600519.SH", "volume": 100, "cost": 10.0}]
        market = FakeMarket(df=daily_df())
        with freeze_time(*OUTSIDE_AFTER):
            res = self._evaluate(pool_rows=[], positions=positions,
                                 market=market, cfg=trade_cfg())
        self.assertEqual(res.get("side"), "hold")
        self.assertNotIn("非买入窗口", res.get("reason", ""))

    def test_disabled_strategy_never_buys_inside_window(self):
        """窗口内但策略未启用 -> hold, 不产生买入"""
        market = FakeMarket(df=daily_df())
        with freeze_time(*INSIDE):
            res = self._evaluate(
                pool_rows=[{"stock_code": "600519.SH", "trade_date": "2026-09-09"}],
                positions=[],
                market=market,
                cfg=trade_cfg(enabled=False),
            )
        self.assertEqual(res.get("side"), "hold")
        self.assertIn("未启用", res.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
