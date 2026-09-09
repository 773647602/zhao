# -*- coding: utf-8 -*-
"""
QMT数据下载脚本
使用xtquant下载贵州茅台股票（600519.SH）的历史数据
保存到本地CSV文件，供后续策略分析使用

注意：运行此脚本需要安装QMT并配置好xtquant
"""
import os
import time
import traceback
import pandas as pd
from xtquant import xtdata


# 数据下载参数配置
STOCK_CODE = '600519.SH'  # 贵州茅台股票代码
STOCK_NAME = '贵州茅台'
DATA_START = '20240101'   # 数据开始日期（需要足够的历史数据计算MACD）
DATA_END = '20251231'     # 数据结束日期


def download_stock_data():
    """
    下载股票历史数据并保存到CSV文件
    """
    print(f"开始下载股票数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期范围：{DATA_START} 至 {DATA_END}")
    print("-" * 60)
    
    try:
        # 步骤1：下载历史数据
        print("步骤1：下载历史数据...")
        try:
            xtdata.download_history_data(
                stock_code=STOCK_CODE,
                period='1d',
                start_time=DATA_START
            )
            print("下载完成，等待数据写入...")
            time.sleep(2)  # 等待数据写入
        except Exception as e:
            print(f"下载数据时出现警告：{e}")
            print("继续尝试获取数据...")
        
        # 步骤2：获取历史数据
        print("\n步骤2：获取历史收盘价数据...")
        res = xtdata.get_market_data(
            stock_list=[STOCK_CODE],
            period='1d',
            start_time=DATA_START,
            end_time='',
            count=-1,
            dividend_type='front',  # 前复权
            fill_data=True
        )
        
        if not res or 'close' not in res or STOCK_CODE not in res['close'].index:
            print("错误：无法获取历史数据")
            return None
        
        # 提取各字段数据
        close_df = res['close']
        open_df = res.get('open', None)
        high_df = res.get('high', None)
        low_df = res.get('low', None)
        volume_df = res.get('volume', None)
        
        # 获取日期列表
        dates = close_df.columns.tolist()
        
        # 构建数据DataFrame
        data_dict = {
            'date': dates,
            'close': close_df.loc[STOCK_CODE].values
        }
        
        if open_df is not None and STOCK_CODE in open_df.index:
            data_dict['open'] = open_df.loc[STOCK_CODE].values
        if high_df is not None and STOCK_CODE in high_df.index:
            data_dict['high'] = high_df.loc[STOCK_CODE].values
        if low_df is not None and STOCK_CODE in low_df.index:
            data_dict['low'] = low_df.loc[STOCK_CODE].values
        if volume_df is not None and STOCK_CODE in volume_df.index:
            data_dict['volume'] = volume_df.loc[STOCK_CODE].values
        
        df = pd.DataFrame(data_dict)
        
        # 转换日期格式
        df['date'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce')
        
        # 过滤掉无效数据
        df = df.dropna(subset=['date', 'close'])
        df = df.sort_values('date').reset_index(drop=True)
        
        print(f"成功获取 {len(df)} 条历史数据")
        print(f"数据日期范围：{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 {df['date'].iloc[-1].strftime('%Y-%m-%d')}")
        
        # 步骤3：保存到CSV文件
        print("\n步骤3：保存数据到CSV文件...")
        output_dir = os.path.join(os.getcwd(), 'data')
        os.makedirs(output_dir, exist_ok=True)
        
        # 保存完整数据
        output_file = os.path.join(output_dir, f'{STOCK_CODE.replace(".", "_")}_daily_QMT.csv')
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
        print("\n提示：现在可以运行 6b-macd_strategy_analysis.py 进行策略分析")
    else:
        print("\n数据下载失败，请检查错误信息。")
