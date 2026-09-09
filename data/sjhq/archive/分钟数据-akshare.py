# -*- coding: utf-8 -*-
"""
分钟数据下载脚本 - AkShare版
使用AkShare下载贵州茅台(600519)的1分钟K线数据
默认下载 2026-02-10 全天数据

运行：python 分钟数据-akshare.py
环境：pip install akshare

注意：AkShare分钟数据(stock_zh_a_hist_min_em)来源于东方财富，
      只能获取最近5个交易日的分钟数据，无需Token，免费使用。
"""
import os
import traceback
import pandas as pd
import akshare as ak


# ============================================================
# 配置
# ============================================================
STOCK_CODE = '600519'          # 贵州茅台（AkShare格式：纯数字）
STOCK_NAME = '贵州茅台'
STOCK_CODE_FULL = '600519.SH'  # 完整代码（用于输出文件名）
TARGET_DATE = '2026-02-10'     # 目标日期
PERIOD = '1'                   # 1分钟（可选：1, 5, 15, 30, 60）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def download_minute_data():
    """
    下载贵州茅台指定日期的1分钟K线数据
    使用 ak.stock_zh_a_hist_min_em 获取分钟数据（来源：东方财富）
    """
    print(f"开始下载分钟数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期：{TARGET_DATE}")
    print(f"周期：{PERIOD}分钟")
    print("-" * 60)

    try:
        # 步骤1：下载分钟数据
        # stock_zh_a_hist_min_em 返回东方财富的分钟K线数据
        # 限制：只能获取最近5个交易日的数据
        start_dt = f"{TARGET_DATE} 09:30:00"
        end_dt = f"{TARGET_DATE} 15:00:00"

        print(f"步骤1：通过AkShare下载{PERIOD}分钟数据...")
        print(f"时间范围：{start_dt} 至 {end_dt}")
        print("（注意：该接口只能获取最近5个交易日的数据）")

        df = ak.stock_zh_a_hist_min_em(
            symbol=STOCK_CODE,
            start_date=start_dt,
            end_date=end_dt,
            period=PERIOD,
            adjust=''         # 不复权（分钟数据一般不做复权）
        )

        if df is None or len(df) == 0:
            print("错误：无法获取分钟数据")
            print("可能原因：")
            print("  1. 目标日期超出最近5个交易日范围")
            print("  2. 目标日期为非交易日")
            print("  3. 网络连接异常")
            return None

        # 步骤2：整理数据格式
        # AkShare返回中文列名，统一重命名为英文
        print("\n步骤2：整理数据格式...")
        col_map = {
            '时间': 'datetime',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
            '最新价': 'latest',
        }
        df = df.rename(columns=col_map)

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
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE_FULL.replace(".", "_")}_1min_akshare.csv')
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
