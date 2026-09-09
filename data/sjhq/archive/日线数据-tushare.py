# -*- coding: utf-8 -*-
"""
日线数据下载脚本 - Tushare版
使用Tushare Pro下载股票日线数据(OHLCV)
保存到本地CSV文件，供后续策略分析使用

注意：运行此脚本需要安装tushare并设置环境变量 TUSHARE_TOKEN
运行：python 日线数据-tushare.py
环境：pip install tushare
"""
import os
import traceback
import pandas as pd
import tushare as ts


# ============================================================
# 数据下载参数配置
# ============================================================
STOCK_CODE = '600519.SH'  # 贵州茅台股票代码（Tushare格式：代码.交易所）
STOCK_NAME = '贵州茅台'
DATA_START = '20240101'   # 数据开始日期
DATA_END = '20251231'     # 数据结束日期


def get_pro():
    """获取 Tushare Pro 实例（需环境变量 TUSHARE_TOKEN）"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token or not str(token).strip():
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN，请先设置后再运行")
    ts.set_token(str(token).strip())
    return ts.pro_api()


def download_stock_data():
    """
    下载股票日线数据并保存到CSV文件
    使用 ts.pro_bar 获取前复权日线数据
    """
    print(f"开始下载股票数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期范围：{DATA_START} 至 {DATA_END}")
    print("-" * 60)

    try:
        # 步骤1：初始化Tushare Pro
        print("步骤1：初始化Tushare Pro...")
        pro = get_pro()
        print("初始化成功")

        # 步骤2：下载日线数据（前复权）
        # ts.pro_bar 是 Tushare 推荐的行情获取方法，支持复权
        print("\n步骤2：下载日线数据（前复权）...")
        df = ts.pro_bar(
            ts_code=STOCK_CODE,
            start_date=DATA_START,
            end_date=DATA_END,
            adj='qfq',   # 前复权（qfq=前复权, hfq=后复权, None=不复权）
            freq='D'      # 日线
        )

        if df is None or len(df) == 0:
            print("错误：无法获取日线数据，请检查Token权限或股票代码")
            return None

        # 整理数据格式，统一列名（与QMT版输出一致）
        df = df.rename(columns={
            'trade_date': 'date',
            'vol': 'volume'
        })
        df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
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

        output_file = os.path.join(output_dir, f'{STOCK_CODE.replace(".", "_")}_daily_tushare.csv')
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
