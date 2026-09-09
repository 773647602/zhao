# -*- coding: utf-8 -*-
"""
日线数据下载脚本 - AkShare版
使用AkShare下载股票日线数据(OHLCV)
保存到本地CSV文件，供后续策略分析使用

注意：AkShare无需Token，直接安装即可使用
运行：python 日线数据-akshare.py
环境：pip install akshare
"""
import os
import traceback
import pandas as pd
import akshare as ak


# ============================================================
# 数据下载参数配置
# ============================================================
STOCK_CODE = '600519'          # 贵州茅台股票代码（AkShare格式：纯数字，不带交易所后缀）
STOCK_NAME = '贵州茅台'
STOCK_CODE_FULL = '600519.SH'  # 完整代码（用于输出文件名，与QMT/Tushare版一致）
DATA_START = '20240101'        # 数据开始日期
DATA_END = '20251231'          # 数据结束日期


def download_stock_data():
    """
    下载股票日线数据并保存到CSV文件
    使用 ak.stock_zh_a_hist 获取前复权日线数据（数据来源：东方财富）
    """
    print(f"开始下载股票数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期范围：{DATA_START} 至 {DATA_END}")
    print("-" * 60)

    try:
        # 步骤1：下载日线数据（前复权）
        # stock_zh_a_hist 从东方财富获取A股历史行情，支持前复权/后复权/不复权
        print("步骤1：通过AkShare下载日线数据（前复权）...")
        df = ak.stock_zh_a_hist(
            symbol=STOCK_CODE,
            period="daily",        # 日线（可选：daily/weekly/monthly）
            start_date=DATA_START,
            end_date=DATA_END,
            adjust="qfq"           # 前复权（qfq=前复权, hfq=后复权, ""=不复权）
        )

        if df is None or len(df) == 0:
            print("错误：无法获取日线数据，请检查股票代码")
            return None

        # 步骤2：整理数据格式
        # AkShare返回中文列名，统一重命名为英文（与QMT/Tushare版输出一致）
        print("\n步骤2：整理数据格式...")
        col_map = {
            '日期': 'date',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
            '振幅': 'amplitude',
            '涨跌幅': 'pct_chg',
            '涨跌额': 'change',
            '换手率': 'turnover',
        }
        df = df.rename(columns=col_map)

        # 转换日期格式
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)

        # 只保留核心列
        keep_cols = ['date', 'open', 'high', 'low', 'close', 'volume']
        df = df[[c for c in keep_cols if c in df.columns]]

        print(f"成功获取 {len(df)} 条日线数据")
        print(f"数据日期范围：{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 {df['date'].iloc[-1].strftime('%Y-%m-%d')}")

        # 步骤3：保存到CSV文件
        print("\n步骤3：保存数据到CSV文件...")
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
        os.makedirs(output_dir, exist_ok=True)

        output_file = os.path.join(output_dir, f'{STOCK_CODE_FULL.replace(".", "_")}_daily_akshare.csv')
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"数据已保存至：{output_file}")

        # 显示数据预览
        print("\n数据预览（前5行）：")
        print(df.head().to_string(index=False))
        print("\n数据预览（后5行）：")
        print(df.tail().to_string(index=False))

        # 显示数据统计
        print("\n数据统计信息：")
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
    result = download_stock_data()

    if result:
        print("\n" + "=" * 60)
        print("数据下载完成!")
        print(f"数据文件：{result}")
        print("=" * 60)
    else:
        print("\n数据下载失败，请检查错误信息。")
