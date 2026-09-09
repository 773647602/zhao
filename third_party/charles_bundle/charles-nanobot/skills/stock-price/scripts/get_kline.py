#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniQMT K 线数据获取脚本
通过 xtquant 获取指定股票的 K 线数据，输出 JSON
"""

import sys
import json
import pandas as pd
from xtquant import xtdata


def get_kline_data(stock_code, period='1d', start_date='', end_date='', count=100):
    """
    获取 MiniQMT K 线数据

    Args:
        stock_code: 股票代码，如 '000001.SZ'
        period: 周期，支持 '1m','5m','15m','30m','1h','1d','1w','1mon'
        start_date: 开始日期 'YYYYMMDD'
        end_date: 结束日期 'YYYYMMDD'
        count: 获取条数
    """
    try:
        xtdata.connect()
        # 1. 先下载历史数据（增量更新）
        xtdata.download_history_data(
            stock_code,
            period=period,
            start_time=start_date if start_date else '19900101',
            end_time=end_date if end_date else '',
            incrementally=True
        )

        # 2. 获取市场数据
        data = xtdata.get_market_data_ex(
            field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
            stock_list=[stock_code],
            period=period,
            start_time=start_date,
            end_time=end_date,
            count=count
        )

        if not data or stock_code not in data:
            return {"error": f"未获取到 {stock_code} 的数据"}
        df = data[stock_code]
        if df is None or len(df) == 0:
            return {"error": f"{stock_code} 无数据"}

        # 3. 数据处理
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            df['date'] = df['time'].dt.strftime('%Y-%m-%d')
            df['datetime'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            df.index = pd.to_datetime(df.index)
            df['date'] = df.index.strftime('%Y-%m-%d')
            df['datetime'] = df.index.strftime('%Y-%m-%d %H:%M:%S')

        for col in ['open', 'high', 'low', 'close']:
            if col in df.columns:
                df[col] = df[col].round(3)

        records = df.to_dict('records')
        records = [{"date": r.get("date", ""), "open": float(r.get("open", 0)),
                   "high": float(r.get("high", 0)), "low": float(r.get("low", 0)),
                   "close": float(r.get("close", 0)), "volume": int(r.get("volume", 0)),
                   "amount": float(r.get("amount", 0))} for r in records]

        return {
            "stock_code": stock_code,
            "period": period,
            "data_count": len(records),
            "data": records[-50:] if len(records) > 50 else records
        }
    except Exception as e:
        return {"error": f"获取失败: {str(e)}"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "请提供股票代码"}, ensure_ascii=False))
        sys.exit(1)
    stock_code = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else '1d'
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    result = get_kline_data(stock_code, period=period, count=count)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
