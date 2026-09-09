# -*- coding: utf-8 -*-
"""
分钟数据下载脚本 - Tushare版
使用Tushare Pro下载贵州茅台(600519.SH)的1分钟K线数据
默认下载 2026-02-10 全天数据

运行：python 分钟数据-tushare.py
环境：pip install tushare，需设置环境变量 TUSHARE_TOKEN

注意：Tushare的分钟数据(1/5/15/30/60min)需要单独开通权限，
      普通积分用户无法获取。需要捐助 1000 元获取分钟行情权限。
      频次限制：每分钟500次，每次最多8000行。
"""
import os
import traceback
import pandas as pd
import tushare as ts


# ============================================================
# 配置
# ============================================================
STOCK_CODE = '600519.SH'       # 贵州茅台（Tushare格式）
STOCK_NAME = '贵州茅台'
TARGET_DATE = '20260210'        # 目标日期
FREQ = '1min'                   # 1分钟（可选：1min, 5min, 15min, 30min, 60min）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def get_pro():
    """获取 Tushare Pro 实例（需环境变量 TUSHARE_TOKEN）"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token or not str(token).strip():
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN，请先设置后再运行")
    ts.set_token(str(token).strip())
    return ts.pro_api()


def download_minute_data():
    """
    下载贵州茅台指定日期的1分钟K线数据
    使用 ts.pro_bar 获取前复权分钟数据
    """
    print(f"开始下载分钟数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期：{TARGET_DATE}")
    print(f"频率：{FREQ}")
    print("-" * 60)

    try:
        # 步骤1：初始化Tushare Pro
        print("步骤1：初始化Tushare Pro...")
        pro = get_pro()
        print("初始化成功")

        # 步骤2：下载分钟数据
        # ts.pro_bar 支持 freq='1min' 获取1分钟数据
        # 注意：分钟数据需要单独开通权限（需捐助1000元）
        print(f"\n步骤2：下载{FREQ}数据（前复权）...")
        df = ts.pro_bar(
            ts_code=STOCK_CODE,
            start_date=TARGET_DATE,
            end_date=TARGET_DATE,
            adj='qfq',
            freq=FREQ
        )

        if df is None or len(df) == 0:
            print("错误：无法获取分钟数据")
            print("可能原因：")
            print("  1. 未开通分钟行情权限（需捐助1000元）")
            print("  2. 目标日期为非交易日")
            print("  3. Token权限不足")
            return None

        # 整理数据格式
        df = df.rename(columns={
            'trade_time': 'datetime',
            'vol': 'volume'
        })

        # 转换时间格式
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime').reset_index(drop=True)

        # 保留核心列
        keep_cols = ['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']
        df = df[[c for c in keep_cols if c in df.columns]]

        print(f"成功获取 {len(df)} 条分钟数据")
        print(f"时间范围：{df['datetime'].iloc[0]} 至 {df['datetime'].iloc[-1]}")

        # 步骤3：保存到CSV文件
        print("\n步骤3：保存数据到CSV文件...")
        os.makedirs(DATA_DIR, exist_ok=True)
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE.replace(".", "_")}_1min_tushare.csv')
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
