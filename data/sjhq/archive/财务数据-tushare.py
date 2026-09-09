# -*- coding: utf-8 -*-
"""
财务数据下载脚本 - Tushare版
使用Tushare Pro下载贵州茅台(600519.SH)的综合财务数据
包含：每股指标、盈利能力、偿债能力、成长能力、营运能力、现金流等常用指标

保存文件（均在 data/ 目录下）：
  - 600519_SH_fina_tushare.csv  -- 综合财务指标（按报告期，多期数据）

运行：python 财务数据-tushare.py
环境：pip install tushare，并设置环境变量 TUSHARE_TOKEN
"""
import os
import traceback
import pandas as pd
import tushare as ts


# ============================================================
# 配置
# ============================================================
STOCK_CODE = '600519.SH'   # 贵州茅台（Tushare格式）
STOCK_NAME = '贵州茅台'

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Tushare fina_indicator 接口 -- 常用财务指标字段
# 文档: https://tushare.pro/document/2?doc_id=79
FINA_FIELDS = ",".join([
    "ts_code", "ann_date", "end_date",

    # ---- 每股指标 ----
    "eps",                    # 基本每股收益
    "dt_eps",                 # 稀释每股收益
    "bps",                    # 每股净资产
    "ocfps",                  # 每股经营现金流
    "undist_profit_ps",       # 每股未分配利润
    "total_revenue_ps",       # 每股营业总收入

    # ---- 盈利能力 ----
    "roe",                    # 净资产收益率(%)
    "roe_waa",                # 加权平均净资产收益率(%)
    "roe_dt",                 # 净资产收益率-扣除非经常损益(%)
    "roa",                    # 总资产报酬率(%)
    "grossprofit_margin",     # 销售毛利率(%)
    "netprofit_margin",       # 销售净利率(%)
    "profit_to_gr",           # 净利润/营业总收入(%)
    "op_of_gr",               # 营业利润/营业总收入(%)
    "ebit_of_gr",             # 息税前利润/营业总收入(%)

    # ---- 偿债能力 ----
    "debt_to_assets",         # 资产负债率(%)
    "current_ratio",          # 流动比率
    "quick_ratio",            # 速动比率
    "cash_ratio",             # 现金比率

    # ---- 成长能力 ----
    "netprofit_yoy",          # 归属母公司股东的净利润同比增长率(%)
    "dt_netprofit_yoy",       # 扣除非经常损益后的净利润同比增长率(%)
    "or_yoy",                 # 营业总收入同比增长率(%)
    "op_yoy",                 # 营业利润同比增长率(%)
    "ocf_yoy",                # 经营活动产生的现金流量净额同比增长率(%)
    "bps_yoy",                # 每股净资产相对年初增长率(%)
    "assets_yoy",             # 总资产相对年初增长率(%)
    "eqt_yoy",                # 净资产相对年初增长率(%)

    # ---- 营运能力 ----
    "assets_turn",            # 总资产周转率(次)
    "inv_turn",               # 存货周转率(次)
    "ar_turn",                # 应收账款周转率(次)
    "ca_turn",                # 流动资产周转率(次)
    "fa_turn",                # 固定资产周转率(次)
    "invturn_days",           # 存货周转天数(天)
    "arturn_days",            # 应收账款周转天数(天)

    # ---- 现金流 ----
    "fcff",                   # 企业自由现金流量(万元)
    "fcfe",                   # 股权自由现金流量(万元)
    "salescash_to_or",        # 销售商品提供劳务收到的现金/营业收入(%)
    "ocf_to_or",              # 经营活动产生的现金流量净额/营业收入(%)
    "ocf_to_opincome",        # 经营活动产生的现金流量净额/经营活动净收益(%)

    # ---- 收益质量 ----
    "op_income",              # 经营活动净收益(万元)
    "ebit",                   # 息税前利润(万元)
    "ebitda",                 # 息税折旧摊销前利润(万元)
])


def get_pro():
    """获取 Tushare Pro 实例（需环境变量 TUSHARE_TOKEN）"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token or not str(token).strip():
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN，请先设置后再运行")
    ts.set_token(str(token).strip())
    return ts.pro_api()


def download_financial_data():
    """
    下载贵州茅台的综合财务指标数据（所有历史报告期）
    """
    print(f"开始下载财务数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print("-" * 60)

    try:
        pro = get_pro()

        # 步骤1：获取综合财务指标
        print("\n步骤1：获取综合财务指标...")
        print(f"  请求字段数：{len(FINA_FIELDS.split(','))}")
        df = pro.fina_indicator(ts_code=STOCK_CODE, fields=FINA_FIELDS)

        if df is None or len(df) == 0:
            print("错误：无法获取财务指标数据，请检查Token权限（需2000积分）")
            return None

        # 按报告期排序（从早到晚）
        df = df.sort_values('end_date').reset_index(drop=True)

        print(f"成功获取 {len(df)} 期财务数据")
        print(f"报告期范围：{df['end_date'].iloc[0]} 至 {df['end_date'].iloc[-1]}")

        # 步骤2：保存到CSV
        print("\n步骤2：保存数据到CSV文件...")
        os.makedirs(DATA_DIR, exist_ok=True)
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE.replace(".", "_")}_fina_tushare.csv')
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"数据已保存至：{output_file}")

        # 步骤3：数据预览与统计
        print("\n" + "=" * 60)
        print("数据预览（最近3期）：")
        print("=" * 60)
        # 转置显示最近3期，方便查看
        recent = df.tail(3).set_index('end_date').T
        print(recent.to_string())

        # 关键指标统计
        print("\n" + "=" * 60)
        print("关键指标最新值：")
        print("=" * 60)
        latest = df.iloc[-1]
        indicators = [
            ("报告期",           latest.get('end_date')),
            ("基本每股收益",     latest.get('eps')),
            ("每股净资产",       latest.get('bps')),
            ("净资产收益率(%)",   latest.get('roe')),
            ("总资产报酬率(%)",   latest.get('roa')),
            ("销售毛利率(%)",     latest.get('grossprofit_margin')),
            ("销售净利率(%)",     latest.get('netprofit_margin')),
            ("资产负债率(%)",     latest.get('debt_to_assets')),
            ("流动比率",         latest.get('current_ratio')),
            ("净利润同比(%)",     latest.get('netprofit_yoy')),
            ("营收同比(%)",       latest.get('or_yoy')),
            ("总资产周转率",     latest.get('assets_turn')),
            ("存货周转率",       latest.get('inv_turn')),
            ("每股经营现金流",   latest.get('ocfps')),
        ]
        for name, val in indicators:
            print(f"  {name:<18s}  {val}")

        # 数据列清单
        print(f"\n数据列（共 {len(df.columns)} 个字段）：")
        for i, col in enumerate(df.columns):
            print(f"  {i+1:>2d}. {col}")

        return output_file

    except Exception as e:
        print(f"下载数据过程中发生错误：{e}")
        traceback.print_exc()
        return None


if __name__ == "__main__":
    result = download_financial_data()

    if result:
        print("\n" + "=" * 60)
        print("财务数据下载完成!")
        print(f"数据文件：{result}")
        print("=" * 60)
    else:
        print("\n财务数据下载失败，请检查错误信息。")
