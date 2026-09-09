"""
miniQMT 交易封装

封装 xt_trader (实盘) 的下单、查询、回调接口。
支持断线重连、心跳保活。
"""

import logging
import time
import threading
from typing import Optional, Callable


class QmtTrader:
    """
    封装本地 miniQMT 交易操作和回调。

    回调签名:
        on_order(order: dict)   -- 委托回报
        on_trade(trade: dict)   -- 成交回报
        on_asset(asset: dict)   -- 资金/持仓变动
    """

    def __init__(self, qmt_path: str, account_id: str):
        self.qmt_path = qmt_path
        self.account_id = account_id
        self.connected = False
        self._xt_trader = None
        self._account = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False

        self.on_order: Optional[Callable[[dict], None]] = None
        self.on_trade: Optional[Callable[[dict], None]] = None
        self.on_asset: Optional[Callable[[dict], None]] = None

        self.logger = logging.getLogger("qmt_agent.trader")

    def connect(self) -> bool:
        """连接国金证券大QMT（通过 bigqmt ZMQ RPC 桥）"""
        try:
            from bigqmt_signal_trader.xtquant_compat import XtQuantTrader, StockAccount

            self.logger.info(f"连接国金大QMT RPC 桥: account={self.account_id}")

            self._xt_trader = XtQuantTrader(account_id=self.account_id)
            self._account = StockAccount(str(self.account_id))

            callback = _QmtCallback(self)
            self._xt_trader.register_callback(callback)

            self._xt_trader.start()
            self.logger.debug("bigqmt start() 成功")

            result = self._xt_trader.connect()
            if result != 0:
                self.logger.error(f"bigqmt RPC 连通失败: code={result}")
                return False
            self.logger.debug("bigqmt connect()（ping）成功")

            time.sleep(1)
            subscribe_result = self._xt_trader.subscribe(self._account)
            if subscribe_result != 0:
                self.logger.error(f"订阅账户失败: code={subscribe_result}")
                return False

            self.connected = True
            self._running = True
            self._start_heartbeat()
            self.logger.info("国金大QMT RPC 桥连接成功")
            return True

        except Exception as e:
            import traceback
            self.logger.error(f"bigqmt RPC 连接异常: {e}")
            self.logger.debug(traceback.format_exc())
            return False

    def disconnect(self):
        self._running = False
        self.connected = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=3)

    def buy(self, code: str, volume: int, price: float = -1) -> str:
        if not self.connected or not self._xt_trader:
            self.logger.warning(f"模拟买入: {code} {volume}股 @{price}")
            return f"sim_buy_{int(time.time())}"

        try:
            from xtquant import xtconstant
            if price <= 0:
                price_type = xtconstant.MARKET_SH_CONVERT_5_LIMIT if code.endswith('.SH') else xtconstant.MARKET_SZ_CONVERT_5_CANCEL
                order_id = self._xt_trader.order_stock(
                    self._account, code, xtconstant.STOCK_BUY,
                    volume, price_type, 0, "市价买入", ""
                )
            else:
                order_id = self._xt_trader.order_stock(
                    self._account, code, xtconstant.STOCK_BUY,
                    volume, xtconstant.FIX_PRICE, price, "限价买入", ""
                )
            self.logger.info(f"买入下单: {code} {volume}股 @{price} → order_id={order_id}")
            return str(order_id)
        except Exception as e:
            self.logger.error(f"买入失败: {e}")
            raise

    def sell(self, code: str, volume: int, price: float = -1) -> str:
        if not self.connected or not self._xt_trader:
            self.logger.warning(f"模拟卖出: {code} {volume}股 @{price}")
            return f"sim_sell_{int(time.time())}"

        try:
            from xtquant import xtconstant
            if price <= 0:
                price_type = xtconstant.MARKET_SH_CONVERT_5_LIMIT if code.endswith('.SH') else xtconstant.MARKET_SZ_CONVERT_5_CANCEL
                order_id = self._xt_trader.order_stock(
                    self._account, code, xtconstant.STOCK_SELL,
                    volume, price_type, 0, "市价卖出", ""
                )
            else:
                order_id = self._xt_trader.order_stock(
                    self._account, code, xtconstant.STOCK_SELL,
                    volume, xtconstant.FIX_PRICE, price, "限价卖出", ""
                )
            self.logger.info(f"卖出下单: {code} {volume}股 @{price} → order_id={order_id}")
            return str(order_id)
        except Exception as e:
            self.logger.error(f"卖出失败: {e}")
            raise

    def cancel_order(self, order_id: str):
        if self._xt_trader:
            try:
                # 大QMT 委托号可能是带前缀的字符串(如 xt1090611960)，不能 int()；
                # compat 层会根据 order_sys_id 解析真实合同号。
                self._xt_trader.cancel_order_stock(self._account, order_id)
                self.logger.info(f"撤单: order_id={order_id}")
            except Exception as e:
                self.logger.error(f"撤单失败: {e}")

    def query_asset(self) -> dict:
        """查询账户资产，返回与 miniqmt_trader_v2 兼容的格式"""
        if not self._xt_trader:
            return {}

        try:
            asset = self._xt_trader.query_stock_asset(self._account)
            if asset is None:
                return {}

            position_profit = float(getattr(asset, 'position_profit', None) or 0.0)
            if position_profit == 0:
                for name in ('profit', 'today_profit', 'total_profit', 'float_pnl'):
                    v = getattr(asset, name, None)
                    if v:
                        try:
                            position_profit = float(v)
                            break
                        except (TypeError, ValueError):
                            pass

            return {
                'total_asset': float(getattr(asset, 'total_asset', 0) or 0),
                'cash': float(getattr(asset, 'cash', 0) or 0),
                'market_value': float(getattr(asset, 'market_value', 0) or 0),
                'frozen_cash': float(getattr(asset, 'frozen_cash', 0) or 0),
                'position_profit': position_profit,
            }
        except Exception as e:
            self.logger.error(f"查询资产失败: {e}")
            return {}

    def query_positions(self) -> list:
        """查询持仓，返回与 miniqmt_trader_v2 兼容的格式"""
        if not self._xt_trader:
            return []

        try:
            positions = self._xt_trader.query_stock_positions(self._account)
            if not positions:
                return []

            out = []
            for p in positions:
                volume = int(getattr(p, 'volume', 0) or 0)
                if volume <= 0:
                    continue

                can_use = int(getattr(p, 'can_use_volume', 0) or 0)
                open_price = float(getattr(p, 'open_price', 0.0) or 0.0)
                market_value = float(getattr(p, 'market_value', 0.0) or 0.0)

                pnl = 0.0
                pnl_pct = 0.0
                today_pnl = 0.0
                today_pnl_pct = 0.0
                cur_price = 0.0
                cost_price = open_price
                last_close = 0.0

                for name in ('profit', 'position_profit', 'float_pnl', 'pnl', 'total_pnl'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            pnl = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('profit_rate', 'pnl_pct', 'float_pnl_pct'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            pnl_pct = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('today_profit', 'today_pnl', 'today_float_pnl'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            today_pnl = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('today_profit_rate', 'today_pnl_pct'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            today_pnl_pct = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('current_price', 'last_price', 'cur_price', 'price', 'market_price'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            cur_price = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('cost_price', 'avg_cost', 'avg_cost_price'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            cost_price = float(v)
                            break
                        except (TypeError, ValueError):
                            pass
                for name in ('last_close', 'pre_close', 'yesterday_close'):
                    v = getattr(p, name, None)
                    if v:
                        try:
                            last_close = float(v)
                            break
                        except (TypeError, ValueError):
                            pass

                out.append({
                    'stock_code': getattr(p, 'stock_code', ''),
                    'volume': volume,
                    'can_use_volume': can_use,
                    'open_price': open_price,
                    'cost_price': cost_price,
                    'cur_price': cur_price,
                    'last_close': last_close,
                    'market_value': market_value,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'today_pnl': today_pnl,
                    'today_pnl_pct': today_pnl_pct,
                    'instrument_name': getattr(p, 'instrument_name', '') or '',
                })
            return out
        except Exception as e:
            self.logger.error(f"查询持仓失败: {e}")
            return []

    def query_orders(self) -> list:
        """查询当日委托，返回 dict 列表"""
        if not self._xt_trader:
            return []

        try:
            orders = self._xt_trader.query_stock_orders(self._account) or []
            out = []
            status_map = {
                48: "未知", 49: "未报", 50: "待报", 51: "已报",
                52: "已报待撤", 53: "部成待撤", 54: "部撤", 55: "已撤",
                56: "部成", 57: "已成", 58: "废单",
            }
            for o in orders:
                side_code = getattr(o, "order_type", 0)
                status_code = getattr(o, "order_status", 0)
                out.append({
                    "order_id": str(getattr(o, "order_id", 0)),
                    "stock_code": getattr(o, "stock_code", ""),
                    "side": "buy" if side_code == 23 else ("sell" if side_code == 24 else f"type_{side_code}"),
                    "order_volume": int(getattr(o, "order_volume", 0) or 0),
                    "traded_volume": int(getattr(o, "traded_volume", 0) or 0),
                    "price": float(getattr(o, "price", 0) or 0),
                    "order_status": status_code,
                    "status_text": status_map.get(status_code, f"未知({status_code})"),
                    "cancelable": status_code in {49, 50, 51, 52, 53},
                    "order_time": getattr(o, "order_time", 0),
                    "strategy_name": getattr(o, "strategy_name", ""),
                    "order_remark": getattr(o, "order_remark", ""),
                })
            return out
        except Exception as e:
            self.logger.error(f"查询委托失败: {e}")
            return []

    def query_account_full(self) -> dict:
        """一次性查询资产、持仓、委托（供云端 request-response 使用）"""
        return {
            "asset": self.query_asset(),
            "positions": self.query_positions(),
            "orders": self.query_orders(),
        }

    def get_realtime_prices(self, code_list: list) -> dict:
        """批量获取股票现价"""
        if not code_list:
            return {}
        result = {c: {"price": 0.0, "last_close": 0.0, "change": 0.0, "change_pct": 0.0} for c in code_list}
        try:
            from bigqmt_signal_trader.xtquant_compat import get_default_client, BigQmtXtData
            xtdata = BigQmtXtData(get_default_client())
            ticks = xtdata.get_full_tick(code_list) or {}
            for code in code_list:
                t = ticks.get(code) or {}
                price = float(t.get("lastPrice") or 0)
                last_close = float(t.get("lastClose") or 0)
                if price > 0 and last_close > 0:
                    change = round(price - last_close, 4)
                    change_pct = round(change / last_close, 4)
                    result[code] = {
                        "price": price,
                        "last_close": last_close,
                        "change": change,
                        "change_pct": change_pct,
                    }
                elif price > 0:
                    result[code] = {"price": price, "last_close": 0.0, "change": 0.0, "change_pct": 0.0}
        except Exception as e:
            self.logger.warning(f"获取实时行情失败: {e}")
        return result

    def health_check(self) -> bool:
        if not self.connected:
            return False
        if self._xt_trader:
            try:
                self._xt_trader.query_stock_asset(self._account)
                return True
            except Exception:
                return False
        return self.connected

    def _start_heartbeat(self):
        def _loop():
            while self._running:
                time.sleep(30)
                if not self._running:
                    break
                if self.health_check():
                    self.logger.debug("心跳正常")
                else:
                    self.logger.warning("心跳检测失败")

        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()


class _QmtCallback:
    def __init__(self, trader: QmtTrader):
        self.trader = trader

    def on_stock_order(self, order):
        if self.trader.on_order:
            self.trader.on_order({
                "order_id": str(getattr(order, "order_id", "")),
                "code": getattr(order, "stock_code", ""),
                "name": getattr(order, "stock_name", "") or getattr(order, "instrument_name", "") or "",
                "status": getattr(order, "order_status", 0),
                "side": "buy" if getattr(order, "order_type", 0) < 30 else "sell",
                "price": getattr(order, "price", 0),
                "volume": getattr(order, "order_volume", 0),
                "filled_volume": getattr(order, "traded_volume", 0),
            })

    def on_stock_trade(self, trade):
        if self.trader.on_trade:
            self.trader.on_trade({
                "order_id": str(getattr(trade, "order_id", "")),
                "code": getattr(trade, "stock_code", ""),
                "price": getattr(trade, "traded_price", 0),
                "volume": getattr(trade, "traded_volume", 0),
                "time": getattr(trade, "traded_time", ""),
            })

    def on_stock_asset(self, asset):
        if self.trader.on_asset:
            self.trader.on_asset({
                "total": getattr(asset, "总资产", 0),
                "available": getattr(asset, "可用资金", 0),
                "frozen": getattr(asset, "冻结资金", 0),
                "market_value": getattr(asset, "持仓市值", 0),
            })

    def on_account_error(self, *args): pass
    def on_error(self, *args): pass
    def on_sys_disconnected(self, *args): pass
    def on_stock_trade_order(self, *args): pass
