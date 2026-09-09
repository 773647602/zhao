# -*- coding: utf-8 -*-
"""
分钟数据下载脚本 - QMT / miniQMT 版
使用xtquant下载贵州茅台(600519.SH)的1分钟K线数据
默认下载 2026-02-10 全天数据

运行：python 分钟数据-QMT.py
环境：需安装QMT并配置好xtquant，运行前需启动miniQMT客户端
"""
import os
import time
import traceback
import pandas as pd
from xtquant import xtdata


# ============================================================
# 配置
# ============================================================
STOCK_CODE = '600519.SH'      # 贵州茅台（QMT格式）
STOCK_NAME = '贵州茅台'
TARGET_DATE = '20260210'       # 目标日期
PERIOD = '1m'                  # 1分钟K线（可选：1m, 5m, 15m, 30m, 60m）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def download_minute_data():
    """
    下载贵州茅台指定日期的1分钟K线数据
    """
    print(f"开始下载分钟数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期：{TARGET_DATE}")
    print(f"周期：{PERIOD}")
    print("-" * 60)

    try:
        # 步骤1：连接QMT
        print("步骤1：连接QMT数据服务...")
        connect_result = xtdata.connect()
        print(f"连接结果：{connect_result}")

        # 步骤2：下载分钟数据
        print("\n步骤2：下载分钟数据...")
        xtdata.download_history_data(
            stock_code=STOCK_CODE,
            period=PERIOD,
            start_time=TARGET_DATE
        )
        time.sleep(2)
        print("下载完成")

        # 步骤3：获取数据
        print("\n步骤3：获取分钟K线数据...")
        # end_time 设为目标日的下一天，确保包含全天数据
        res = xtdata.get_market_data(
            stock_list=[STOCK_CODE],
            period=PERIOD,
            start_time=TARGET_DATE,
            end_time='20260211',
            count=-1,
            dividend_type='front',
            fill_data=False
        )

        if not res or 'close' not in res or STOCK_CODE not in res['close'].index:
            print("错误：无法获取分钟数据")
            return None

        # 提取各字段
        close_df = res['close']
        open_df = res.get('open', None)
        high_df = res.get('high', None)
        low_df = res.get('low', None)
        volume_df = res.get('volume', None)
        amount_df = res.get('amount', None)

        timestamps = close_df.columns.tolist()

        data_dict = {
            'datetime': timestamps,
            'close': close_df.loc[STOCK_CODE].values,
        }
        if open_df is not None and STOCK_CODE in open_df.index:
            data_dict['open'] = open_df.loc[STOCK_CODE].values
        if high_df is not None and STOCK_CODE in high_df.index:
            data_dict['high'] = high_df.loc[STOCK_CODE].values
        if low_df is not None and STOCK_CODE in low_df.index:
            data_dict['low'] = low_df.loc[STOCK_CODE].values
        if volume_df is not None and STOCK_CODE in volume_df.index:
            data_dict['volume'] = volume_df.loc[STOCK_CODE].values
        if amount_df is not None and STOCK_CODE in amount_df.index:
            data_dict['amount'] = amount_df.loc[STOCK_CODE].values

        df = pd.DataFrame(data_dict)

        # 转换时间格式（QMT分钟数据的时间戳格式为 YYYYMMDDHHmmss 或纯数字）
        df['datetime'] = pd.to_datetime(df['datetime'].astype(str), format='%Y%m%d%H%M%S', errors='coerce')
        df = df.dropna(subset=['datetime', 'close'])

        # 只保留目标日期的数据
        target_date_dt = pd.Timestamp(TARGET_DATE)
        df = df[df['datetime'].dt.date == target_date_dt.date()]
        df = df.sort_values('datetime').reset_index(drop=True)

        if len(df) == 0:
            print(f"警告：{TARGET_DATE} 没有分钟数据（可能是非交易日）")
            return None

        print(f"成功获取 {len(df)} 条分钟数据")
        print(f"时间范围：{df['datetime'].iloc[0]} 至 {df['datetime'].iloc[-1]}")

        # 步骤4：保存到CSV
        print("\n步骤4：保存数据到CSV文件...")
        os.makedirs(DATA_DIR, exist_ok=True)
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE.replace(".", "_")}_1min_QMT.csv')
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"数据已保存至：{output_file}")

        # 数据预览
        print("\n数据预览（前10行）：")
        print(df.head(10).to_string(index=False))
        print(f"\n数据预览（后5行）：")
        print(df.tail(5).to_string(index=False))

        # 统计信息
        print(f"\n数据统计：")
        print(f"  总记录数：{len(df)}")
        print(f"  收盘价范围：{df['close'].min():.2f} - {df['close'].max():.2f}")
        if 'volume' in df.columns:
            print(f"  成交量范围：{df['volume'].min():,.0f} - {df['volume'].max():,.0f}")

        return output_file

    except Exception as e:
        print(f"下载数据过程中发生错误：{e}")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    result = download_minute_data()

    if result:
        print("\n" + "=" * 60)
        print("分钟数据下载完成!")
        print(f"数据文件：{result}")
        print("=" * 60)
    else:
        print("\n分钟数据下载失败，请检查错误信息。")
