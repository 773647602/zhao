"""
qmt_agent 消息路由器

职责：
  - 接收来自任意 WsClient 的消息，标记来源
  - 分发到对应的 handler 处理
  - 回复消息时发送回来源 WsClient
  - 交易回报/行情数据广播到所有连接
"""

import logging
from typing import Optional

from ws_client import WsClient
from connection_manager import ConnectionManager


class MessageRouter:
    """消息路由：请求-回复（发回来源），事件-广播（发到所有连接）"""

    def __init__(
        self,
        conn_mgr: ConnectionManager,
        trader,
        uploader,
    ):
        self.conn_mgr = conn_mgr
        self.trader = trader
        self.uploader = uploader
        self.logger = logging.getLogger("qmt_agent.router")

    def route(self, source_client: WsClient, msg: dict):
        """路由消息到对应处理器

        Args:
            source_client: 消息来源的 WsClient（回复时发回此连接）
            msg: 消息体
        """
        msg_type = msg.get("type", "")
        self.logger.debug(f"路由消息: {msg_type}")

        # ---- 请求-回复类消息（回复发回来源）----
        if msg_type == "conn_register":
            self.logger.info(
                f"连接注册成功: conn_id={msg.get('conn_id')} "
                f"account_id={msg.get('account_id')} message={msg.get('message')}"
            )

        elif msg_type == "order":
            self._handle_order(source_client, msg)

        elif msg_type == "cancel_order":
            self._handle_cancel_order(source_client, msg)

        elif msg_type == "query_positions":
            self._report_positions(source_client)

        elif msg_type == "query_account":
            self._handle_query_account(source_client, msg)

        elif msg_type == "get_kline":
            self._handle_get_kline(source_client, msg)

        elif msg_type == "get_tick":
            self._handle_get_tick(source_client, msg)

        elif msg_type == "get_stock_names":
            self._handle_get_stock_names(source_client, msg)

        elif msg_type == "get_sector_list":
            self._handle_get_sector_list(source_client, msg)

        elif msg_type == "get_stock_list_in_sector":
            self._handle_get_stock_list_in_sector(source_client, msg)

        elif msg_type == "get_instrument_detail":
            self._handle_get_instrument_detail(source_client, msg)

        elif msg_type == "get_stock_list":
            self._handle_get_stock_list(source_client, msg)

        elif msg_type == "get_kline_batch":
            self._handle_get_kline_batch(source_client, msg)

        elif msg_type == "get_minute_kline_batch":
            self._handle_get_minute_kline_batch(source_client, msg)

        elif msg_type == "get_instrument_detail_batch":
            self._handle_get_instrument_detail_batch(source_client, msg)

        elif msg_type == "get_trading_dates":
            self._handle_get_trading_dates(source_client, msg)

        elif msg_type == "download_history_data":
            self._handle_download_history_data(source_client, msg)

        elif msg_type == "heartbeat":
            source_client.send({"type": "heartbeat_ack"})

        elif msg_type == "update_stocks":
            stocks = msg.get("stocks", [])
            self.uploader.update_codes(stocks)

    # ---- 交易指令处理 ----

    def _handle_order(self, source_client: WsClient, msg: dict):
        code = msg["symbol"]
        volume = msg["volume"]
        price = msg.get("price", -1)
        side = msg["side"]
        ref = msg.get("ref", "")

        try:
            if side == "buy":
                order_id = self.trader.buy(code, volume, price)
            else:
                order_id = self.trader.sell(code, volume, price)
            source_client.send({"type": "order_ack", "order_id": order_id, "ref": ref})
        except Exception as e:
            source_client.send({"type": "order_error", "ref": ref, "error": str(e)})

    def _handle_cancel_order(self, source_client: WsClient, msg: dict):
        order_id = msg.get("order_id", "")
        try:
            self.trader.cancel_order(order_id)
            source_client.send({"type": "cancel_ack", "order_id": order_id})
        except Exception as e:
            source_client.send({"type": "cancel_error", "order_id": order_id, "error": str(e)})

    def _report_positions(self, source_client: WsClient):
        pos = self.trader.query_positions()
        asset = self.trader.query_asset()
        source_client.send({
            "type": "positions_report",
            "positions": pos,
            "asset": asset,
        })

    def _handle_query_account(self, source_client: WsClient, msg: dict):
        """处理查询账户资产/持仓/委托的请求（request-response 模式）"""
        request_id = msg.get("request_id", "")
        try:
            data = self.trader.query_account_full()
            code_list = [p.get("stock_code") for p in data.get("positions", []) if p.get("stock_code")]
            if code_list:
                rt_prices = self.trader.get_realtime_prices(code_list)
                for p in data.get("positions", []):
                    code = p.get("stock_code", "")
                    rt = rt_prices.get(code) or {}
                    price = float(rt.get("price") or 0.0)
                    last_close = float(rt.get("last_close") or 0.0)
                    change = float(rt.get("change") or 0.0)
                    change_pct = float(rt.get("change_pct") or 0.0)
                    vol_total = float(p.get("volume") or 0)
                    can_use = float(p.get("can_use_volume") or 0)
                    cur_price = price if price > 0 else float(p.get("cur_price") or 0.0)
                    cost = float(p.get("cost_price") or p.get("open_price") or 0.0)
                    p["cur_price"] = round(cur_price, 3)
                    p["last_close"] = round(last_close, 4)
                    p["today_change"] = round(change, 4)
                    p["today_change_pct"] = round(change_pct, 4)
                    p["cost"] = round(cost, 3)

                    if cur_price > 0 and cost > 0:
                        pnl_val = round((cur_price - cost) * vol_total, 2)
                        pnl_pct_val = round((cur_price - cost) / cost, 4)
                    else:
                        pnl_val = 0.0
                        pnl_pct_val = 0.0
                    p["pnl"] = pnl_val
                    p["pnl_pct"] = pnl_pct_val

                    qmt_today_pnl = float(p.get("today_pnl") or 0.0)
                    if qmt_today_pnl != 0:
                        pass
                    elif cur_price > 0 and last_close > 0:
                        if can_use < vol_total and cost > 0:
                            p["today_pnl"] = pnl_val
                        else:
                            p["today_pnl"] = round(change * vol_total, 2)
                    else:
                        p["today_pnl"] = 0.0

                    mv = float(p.get("market_value") or 0.0)
                    if mv <= 0 and cur_price > 0:
                        mv = cur_price * vol_total
                    p["market_value"] = round(mv, 2)
            source_client.send({
                "type": "account_data",
                "request_id": request_id,
                "data": data,
            })
        except Exception as e:
            self.logger.error(f"查询账户失败: {e}")
            source_client.send({
                "type": "account_data",
                "request_id": request_id,
                "error": str(e),
            })

    # ---- 行情数据查询 ----

    def _handle_get_kline(self, source_client: WsClient, msg: dict):
        """处理 K 线数据查询请求（分钟级），通过 xtquant 获取并返回"""
        code = msg.get("code", "")
        period = msg.get("period", "1d")
        request_id = msg.get("request_id", "")
        start_time = msg.get("start_time", "")
        end_time = msg.get("end_time", "")
        count = msg.get("count", None)

        try:
            from xtquant import xtdata
            from datetime import date, timedelta, datetime

            PERIOD_CONFIG = {
                '1m':  {'days': 3,   'default_count': 500,   'bars_per_day': 240},
                '5m':  {'days': 10,  'default_count': 240,   'bars_per_day': 48},
                '15m': {'days': 20,  'default_count': 160,   'bars_per_day': 16},
                '60m': {'days': 30,  'default_count': 120,   'bars_per_day': 4},
                '1d':  {'days': 250, 'default_count': 500,   'bars_per_day': 1},
            }
            cfg = PERIOD_CONFIG.get(period, {'days': 10, 'default_count': 200, 'bars_per_day': 1})

            if end_time and len(end_time) >= 8:
                end = end_time[:8]
            else:
                end = date.today().strftime("%Y%m%d")

            if start_time and len(start_time) >= 8:
                start = start_time[:8]
            else:
                start = (date.today() - timedelta(days=cfg['days'])).strftime("%Y%m%d")

            if count is None or count <= 0:
                try:
                    start_dt = datetime.strptime(start, "%Y%m%d")
                    end_dt = datetime.strptime(end, "%Y%m%d")
                    days_diff = (end_dt - start_dt).days
                    est_bars = int(days_diff * 250 / 365 * cfg['bars_per_day'])
                    count = max(est_bars, cfg['default_count'])
                except Exception:
                    count = cfg['default_count']

            xtdata.download_history_data(code, period, start_time=start, end_time=end)
            raw = xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=[code],
                period=period,
                start_time=start,
                end_time=end,
                count=count,
                dividend_type="front",
            )

            if not raw or code not in raw:
                source_client.send({"type": "kline_data", "request_id": request_id, "data": []})
                return

            data = []
            df = raw[code]
            if df is not None and len(df) > 0:
                for idx, row in df.iterrows():
                    idx_str = str(idx)
                    if len(idx_str) < 8:
                        continue
                    date_part = f"{idx_str[:4]}-{idx_str[4:6]}-{idx_str[6:8]}"
                    time_part = ""
                    if len(idx_str) >= 12:
                        time_part = f" {idx_str[8:10]}:{idx_str[10:12]}"
                    data.append({
                        "time": date_part + time_part,
                        "open": float(row.get("open", 0) or 0),
                        "high": float(row.get("high", 0) or 0),
                        "low": float(row.get("low", 0) or 0),
                        "close": float(row.get("close", 0) or 0),
                        "volume": int(row.get("volume", 0) or 0),
                        "amount": float(row.get("amount", 0) or 0),
                    })

            source_client.send({"type": "kline_data", "request_id": request_id, "data": data})
        except ImportError:
            source_client.send({"type": "kline_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取 K 线数据失败: {e}")
            source_client.send({"type": "kline_data", "request_id": request_id, "error": str(e)})

    def _handle_get_tick(self, source_client: WsClient, msg: dict):
        """处理分时数据查询请求，通过 xtquant 获取并返回"""
        code = msg.get("code", "")
        request_id = msg.get("request_id", "")

        try:
            from xtquant import xtdata
            from datetime import datetime

            ticks = []
            today = datetime.now().strftime("%Y%m%d")

            try:
                raw = xtdata.get_market_data_ex(
                    field_list=["open", "high", "low", "close", "volume"],
                    stock_list=[code],
                    period='1m',
                    start_time=today,
                    end_time=today,
                )
                df = raw.get(code) if isinstance(raw, dict) else raw
                if df is not None and hasattr(df, 'empty') and not df.empty:
                    for idx, row in df.iterrows():
                        try:
                            if isinstance(idx, str):
                                time_str = f"{idx[8:10]}:{idx[10:12]}:{idx[12:14]}"
                            else:
                                ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                                time_str = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else ""
                            close = float(row.get("close") or row.get("open") or 0)
                            if close <= 0:
                                continue
                            ticks.append({
                                "time": time_str,
                                "lastPrice": close,
                                "volume": int(row.get("volume") or 0),
                                "lastClose": 0.0,
                                "bidPrice": [],
                                "askPrice": [],
                            })
                        except Exception:
                            pass
            except Exception as e:
                self.logger.warning(f"获取 tick 历史数据异常: {e}")

            if len(ticks) < 2:
                try:
                    full_tick = xtdata.get_full_tick([code]) or {}
                    ft = full_tick.get(code) or {}
                    price = ft.get("lastPrice") or 0
                    last_close = ft.get("lastClose") or 0
                    timetag = ft.get("timetag") or ""
                    time_str = timetag[9:17].strip() if len(timetag) > 8 else ""
                    if price > 0:
                        ticks.append({
                            "time": time_str,
                            "lastPrice": float(price),
                            "volume": int(ft.get("volume") or 0),
                            "lastClose": float(last_close) if last_close else float(price),
                            "bidPrice": ft.get("bidPrice") or [],
                            "askPrice": ft.get("askPrice") or [],
                        })
                except Exception as e:
                    self.logger.warning(f"get_full_tick 兜底异常: {e}")

            source_client.send({"type": "tick_data", "request_id": request_id, "ticks": ticks})
        except ImportError:
            source_client.send({"type": "tick_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取 tick 数据失败: {e}")
            source_client.send({"type": "tick_data", "request_id": request_id, "error": str(e)})

    def _handle_get_stock_names(self, source_client: WsClient, msg: dict):
        """处理股票名称批量查询请求"""
        codes = msg.get("codes", [])
        request_id = msg.get("request_id", "")

        try:
            from xtquant import xtdata
            names = {}
            for code in codes:
                try:
                    detail = xtdata.get_instrument_detail(code)
                    names[code] = detail.get("InstrumentName", "") if detail else ""
                except Exception:
                    names[code] = ""
            source_client.send({"type": "stock_names_data", "request_id": request_id, "names": names})
        except ImportError:
            source_client.send({"type": "stock_names_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取股票名称失败: {e}")
            source_client.send({"type": "stock_names_data", "request_id": request_id, "error": str(e)})

    def _handle_get_sector_list(self, source_client: WsClient, msg: dict):
        """获取所有板块列表"""
        request_id = msg.get("request_id", "")
        try:
            from xtquant import xtdata
            sectors = xtdata.get_sector_list() or []
            source_client.send({
                "type": "sector_list_data",
                "request_id": request_id,
                "sectors": sectors,
            })
        except ImportError:
            source_client.send({"type": "sector_list_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取板块列表失败: {e}")
            source_client.send({"type": "sector_list_data", "request_id": request_id, "error": str(e)})

    def _handle_get_stock_list_in_sector(self, source_client: WsClient, msg: dict):
        """获取指定板块的成分股列表"""
        request_id = msg.get("request_id", "")
        sector = msg.get("sector", "")
        if not sector:
            source_client.send({"type": "stock_list_data", "request_id": request_id, "error": "缺少 sector 参数"})
            return
        try:
            from xtquant import xtdata
            codes = xtdata.get_stock_list_in_sector(sector) or []
            source_client.send({
                "type": "stock_list_data",
                "request_id": request_id,
                "sector": sector,
                "codes": codes,
            })
        except ImportError:
            source_client.send({"type": "stock_list_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取板块成分股失败: {e}")
            source_client.send({"type": "stock_list_data", "request_id": request_id, "error": str(e)})

    def _handle_get_instrument_detail(self, source_client: WsClient, msg: dict):
        """获取单只股票的详细信息"""
        request_id = msg.get("request_id", "")
        code = msg.get("code", "")
        if not code:
            source_client.send({"type": "instrument_detail_data", "request_id": request_id, "error": "缺少 code 参数"})
            return
        try:
            from xtquant import xtdata
            detail = xtdata.get_instrument_detail(code) or {}
            source_client.send({
                "type": "instrument_detail_data",
                "request_id": request_id,
                "code": code,
                "detail": detail,
            })
        except ImportError:
            source_client.send({"type": "instrument_detail_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取股票详情失败: {e}")
            source_client.send({"type": "instrument_detail_data", "request_id": request_id, "error": str(e)})

    def _handle_get_stock_list(self, source_client: WsClient, msg: dict):
        """获取板块成分股列表（如 沪深A股、沪深300 等）"""
        request_id = msg.get("request_id", "")
        sector = msg.get("sector", "沪深A股")
        try:
            from xtquant import xtdata
            codes = xtdata.get_stock_list_in_sector(sector) or []
            source_client.send({
                "type": "stock_list_data",
                "request_id": request_id,
                "sector": sector,
                "codes": codes,
            })
        except ImportError:
            source_client.send({"type": "stock_list_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取股票列表失败: {e}")
            source_client.send({"type": "stock_list_data", "request_id": request_id, "error": str(e)})

    def _handle_get_kline_batch(self, source_client: WsClient, msg: dict):
        """批量下载 K 线数据 — 2026-07-06 优化: 用 stock_list 真批量替代逐个循环

        之前逐个 code 循环调 xtdata.get_market_data_ex(stock_list=[code])，N 股 = N 次调用。
        改为一次传 batch 个 codes，xtdata 内部一次性返回所有股的数据。
        """
        request_id = msg.get("request_id", "")
        codes = msg.get("codes", [])
        period = msg.get("period", "1d")
        start_time = msg.get("start_time", "")
        end_time = msg.get("end_time", "")
        batch_size = msg.get("batch_size", 30)

        if not codes:
            source_client.send({"type": "kline_batch_data", "request_id": request_id, "error": "缺少 codes 参数"})
            return

        try:
            from xtquant import xtdata
            import time

            t_start = time.time()
            all_data = {}
            total = len(codes)
            for i in range(0, total, batch_size):
                batch = codes[i:i + batch_size]
                try:
                    for code in batch:
                        try:
                            xtdata.download_history_data(
                                code, period, start_time=start_time,
                                end_time=end_time, incrementally=True,
                            )
                        except Exception as e:
                            self.logger.warning(f"download {code} 失败: {e}")
                    raw = xtdata.get_market_data_ex(
                        field_list=["open", "high", "low", "close", "volume", "amount"],
                        stock_list=batch,
                        period=period,
                        start_time=start_time,
                        end_time=end_time,
                        dividend_type="front",
                    )
                    if raw:
                        for code in batch:
                            df = raw.get(code)
                            if df is None or len(df) == 0:
                                continue
                            kline_rows = []
                            for idx, row in df.iterrows():
                                idx_str = str(idx)
                                if len(idx_str) < 8:
                                    continue
                                date_part = f"{idx_str[:4]}-{idx_str[4:6]}-{idx_str[6:8]}"
                                time_part = ""
                                if len(idx_str) >= 12:
                                    time_part = f" {idx_str[8:10]}:{idx_str[10:12]}"
                                kline_rows.append({
                                    "time": date_part + time_part,
                                    "open": float(row.get("open", 0) or 0),
                                    "high": float(row.get("high", 0) or 0),
                                    "low": float(row.get("low", 0) or 0),
                                    "close": float(row.get("close", 0) or 0),
                                    "volume": int(row.get("volume", 0) or 0),
                                    "amount": float(row.get("amount", 0) or 0),
                                })
                            if kline_rows:
                                all_data[code] = kline_rows
                except Exception as e:
                    self.logger.warning(f"批量 {i}-{i+len(batch)} 失败: {e}")
                self.logger.info(f"[get_kline_batch] 进度: {min(i + batch_size, total)}/{total}")

            self.logger.info(
                "[get_kline_batch] 优化版: %d codes / %d batches | %.3fs | 返回 %d codes",
                total, (total + batch_size - 1) // batch_size,
                time.time() - t_start, len(all_data),
            )
            source_client.send({
                "type": "kline_batch_data",
                "request_id": request_id,
                "data": all_data,
            })
        except ImportError:
            source_client.send({"type": "kline_batch_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"批量下载 K 线失败: {e}")
            source_client.send({"type": "kline_batch_data", "request_id": request_id, "error": str(e)})

    def _handle_get_minute_kline_batch(self, source_client: WsClient, msg: dict):
        """批量获取分钟 K 线数据 — 2026-07-06 优化: 按 (date) 分组，每日期一次 stock_list 真批量

        之前逐个 pair 循环 N×M 次 xtdata 调用。
        改为按 date 分组：同一天多只股票一次 stock_list 拉完，xtdata 内部共享行情查询非常高效；
        不同日期再串行 (xtdata 非线程安全)。
        """
        request_id = msg.get("request_id", "")
        pairs = msg.get("pairs", [])  # [{"code": "600699.SH", "date": "20260625"}, ...]

        if not pairs:
            source_client.send({"type": "minute_kline_batch_data", "request_id": request_id, "error": "缺少 pairs 参数"})
            return

        try:
            from xtquant import xtdata
            import time
            from collections import defaultdict

            t_start = time.time()
            all_data = {}

            by_date = defaultdict(list)
            for item in pairs:
                code = item.get("code", "")
                date_str = item.get("date", "")
                if code and date_str:
                    by_date[date_str].append(code)

            for date_str, codes in by_date.items():
                for code in codes:
                    try:
                        xtdata.download_history_data(
                            code, "1m",
                            start_time=date_str, end_time=date_str,
                            incrementally=True,
                        )
                    except Exception as e:
                        self.logger.warning(f"[分钟线-BATCH] download {code} {date_str} 失败: {e}")
                try:
                    raw = xtdata.get_market_data_ex(
                        field_list=["open", "high", "low", "close", "volume", "amount"],
                        stock_list=codes,
                        period="1m",
                        start_time=date_str,
                        end_time=date_str,
                        dividend_type="front",
                    )
                    if raw:
                        for code in codes:
                            df = raw.get(code)
                            if df is None or len(df) == 0:
                                continue
                            bars = []
                            for idx, row in df.iterrows():
                                idx_str = str(idx)
                                if len(idx_str) < 12:
                                    continue
                                date_part = f"{idx_str[:4]}-{idx_str[4:6]}-{idx_str[6:8]}"
                                time_part = f" {idx_str[8:10]}:{idx_str[10:12]}"
                                bars.append({
                                    "time": date_part + time_part,
                                    "open": float(row.get("open", 0) or 0),
                                    "high": float(row.get("high", 0) or 0),
                                    "low": float(row.get("low", 0) or 0),
                                    "close": float(row.get("close", 0) or 0),
                                    "volume": int(row.get("volume", 0) or 0),
                                    "amount": float(row.get("amount", 0) or 0),
                                })
                            if bars:
                                all_data[f"{code}|{date_str}"] = bars
                except Exception as e:
                    self.logger.warning("[分钟线-BATCH] %s 真批量失败: %s", date_str, e)

            self.logger.info(
                "[get_minute_kline_batch] 优化版: %d pairs / %d dates | %.3fs | 返回 %d/%d",
                len(pairs), len(by_date), time.time() - t_start, len(all_data), len(pairs),
            )
            source_client.send({
                "type": "minute_kline_batch_data",
                "request_id": request_id,
                "data": all_data,
            })
        except ImportError:
            source_client.send({"type": "minute_kline_batch_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"批量获取分钟 K 线失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            source_client.send({"type": "minute_kline_batch_data", "request_id": request_id, "error": str(e)})

    def _handle_get_instrument_detail_batch(self, source_client: WsClient, msg: dict):
        """批量获取股票详细信息"""
        request_id = msg.get("request_id", "")
        codes = msg.get("codes", [])

        if not codes:
            source_client.send({"type": "instrument_detail_batch_data", "request_id": request_id, "error": "缺少 codes 参数"})
            return

        try:
            from xtquant import xtdata
            details = {}
            for code in codes:
                try:
                    detail = xtdata.get_instrument_detail(code) or {}
                    details[code] = detail
                except Exception:
                    details[code] = {}
            source_client.send({
                "type": "instrument_detail_batch_data",
                "request_id": request_id,
                "details": details,
            })
        except ImportError:
            source_client.send({"type": "instrument_detail_batch_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"批量获取证券详情失败: {e}")
            source_client.send({"type": "instrument_detail_batch_data", "request_id": request_id, "error": str(e)})

    def _handle_get_trading_dates(self, source_client: WsClient, msg: dict):
        """获取交易日列表"""
        request_id = msg.get("request_id", "")
        exchange = msg.get("exchange", "SH")
        start_time = msg.get("start_time", "")
        end_time = msg.get("end_time", "")
        try:
            from xtquant import xtdata
            dates = xtdata.get_trading_dates(exchange, start_time=start_time, end_time=end_time) or []
            formatted = []
            for d in dates:
                s = str(d)
                if "-" in s:
                    formatted.append(s.replace("-", ""))
                elif s.isdigit() and len(s) == 14:
                    formatted.append(s[:8])
                elif s.isdigit() and len(s) >= 12:
                    formatted.append(s[:8])
                else:
                    formatted.append(s)
            source_client.send({
                "type": "trading_dates_data",
                "request_id": request_id,
                "dates": formatted,
            })
        except ImportError:
            source_client.send({"type": "trading_dates_data", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"获取交易日列表失败: {e}")
            source_client.send({"type": "trading_dates_data", "request_id": request_id, "error": str(e)})

    def _handle_download_history_data(self, source_client: WsClient, msg: dict):
        """下载单只股票历史 K 线数据到 miniQMT 本地缓存"""
        request_id = msg.get("request_id", "")
        code = msg.get("code", "")
        period = msg.get("period", "1d")
        start_time = msg.get("start_time", "")
        end_time = msg.get("end_time", "")
        if not code:
            source_client.send({"type": "download_history_data_result", "request_id": request_id, "error": "缺少 code 参数"})
            return
        try:
            from xtquant import xtdata
            xtdata.download_history_data(code, period, start_time=start_time, end_time=end_time, incrementally=True)
            source_client.send({
                "type": "download_history_data_result",
                "request_id": request_id,
                "ok": True,
            })
        except ImportError:
            source_client.send({"type": "download_history_data_result", "request_id": request_id, "error": "xtquant 未安装"})
        except Exception as e:
            self.logger.error(f"下载历史数据失败: {e}")
            source_client.send({"type": "download_history_data_result", "request_id": request_id, "error": str(e)})

    # ---- 广播方法 ----

    def broadcast(self, data: dict):
        """广播消息到所有在线连接"""
        self.conn_mgr.broadcast(data)