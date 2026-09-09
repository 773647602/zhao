# -*- coding: utf-8 -*-
"""
财务数据下载脚本 - AkShare版
使用AkShare下载贵州茅台(600519)的综合财务数据
数据来源：新浪财经（三大财务报表：利润表、资产负债表、现金流量表）
从原始报表中提取并计算常用财务指标

保存文件（均在 data/ 目录下）：
  - 600519_SH_fina_akshare.csv  -- 综合财务指标（按报告期，多期数据）

注意：AkShare无需Token，直接安装即可使用
运行：python 财务数据-akshare.py
环境：pip install akshare
"""
import os
import traceback
import pandas as pd
import akshare as ak


# ============================================================
# 配置
# ============================================================
STOCK_CODE_SINA = 'sh600519'   # 贵州茅台（新浪格式：sh/sz + 代码）
STOCK_NAME = '贵州茅台'
STOCK_CODE_FULL = '600519.SH'  # 完整代码（用于输出文件名）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def safe_float(val):
    """安全转换为浮点数"""
    try:
        if val is None or str(val).strip() in ('', '--', 'None', 'nan'):
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_divide(a, b, pct=False):
    """安全除法，b为0时返回None。pct=True时结果乘以100"""
    if a is None or b is None:
        return None
    a, b = float(a), float(b)
    if b == 0:
        return None
    result = a / b
    if pct:
        result *= 100
    return round(result, 4)


def get_col(row, col_names, default=None):
    """
    从行中获取字段值，支持多个候选列名。
    使用 'in' 进行模糊匹配，避免列名包含额外字符时匹配不上。
    """
    for name in col_names:
        # 精确匹配
        if name in row.index:
            val = safe_float(row[name])
            if val is not None:
                return val
        # 模糊匹配：列名包含关键字
        for col in row.index:
            if name in str(col):
                val = safe_float(row[col])
                if val is not None:
                    return val
    return default


def download_financial_data():
    """
    下载贵州茅台的三大财务报表，提取并计算常用财务指标。
    使用 ak.stock_financial_report_sina 一次获取单只股票全部历史报告期。
    """
    print(f"开始下载财务数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE_SINA})")
    print("-" * 60)

    try:
        # ============================================================
        # 步骤1：下载三大财务报表（每个调用获取该股票全部历史报告期）
        # ============================================================

        # --- 利润表 ---
        print("步骤1：下载利润表...")
        df_income = ak.stock_financial_report_sina(stock=STOCK_CODE_SINA, symbol="利润表")
        print(f"  利润表：{len(df_income)} 期")
        print(f"  列名：{df_income.columns.tolist()[:10]}...")

        # --- 资产负债表 ---
        print("\n步骤2：下载资产负债表...")
        df_balance = ak.stock_financial_report_sina(stock=STOCK_CODE_SINA, symbol="资产负债表")
        print(f"  资产负债表：{len(df_balance)} 期")
        print(f"  列名：{df_balance.columns.tolist()[:10]}...")

        # --- 现金流量表 ---
        print("\n步骤3：下载现金流量表...")
        df_cashflow = ak.stock_financial_report_sina(stock=STOCK_CODE_SINA, symbol="现金流量表")
        print(f"  现金流量表：{len(df_cashflow)} 期")
        print(f"  列名：{df_cashflow.columns.tolist()[:10]}...")

        # ============================================================
        # 步骤4：提取并计算财务指标
        # ============================================================
        print("\n步骤4：提取并计算财务指标...")

        # 用第一列作为报告期（stock_financial_report_sina返回的第一列始终是日期）
        # 避免因列名编码差异导致匹配失败
        def normalize_date_col(df):
            """将DataFrame的第一列统一命名为date_col，并标准化日期格式"""
            date_col_name = df.columns[0]
            df = df.copy()
            df['_date'] = df[date_col_name].astype(str).str.replace('-', '').str[:8]
            return df

        df_income = normalize_date_col(df_income)
        df_balance = normalize_date_col(df_balance)
        df_cashflow = normalize_date_col(df_cashflow)

        # 按报告期索引
        income_map = {}
        for _, row in df_income.iterrows():
            period = row['_date']
            if period and len(period) == 8 and period.isdigit():
                income_map[period] = row

        balance_map = {}
        for _, row in df_balance.iterrows():
            period = row['_date']
            if period and len(period) == 8 and period.isdigit():
                balance_map[period] = row

        cashflow_map = {}
        for _, row in df_cashflow.iterrows():
            period = row['_date']
            if period and len(period) == 8 and period.isdigit():
                cashflow_map[period] = row

        # 取所有报告期的交集
        all_periods = sorted(set(income_map.keys()) & set(balance_map.keys()))
        print(f"  共 {len(all_periods)} 个报告期")

        records = []
        for period in all_periods:
            inc = income_map.get(period)
            bal = balance_map.get(period)
            cf = cashflow_map.get(period)

            # --- 从利润表提取 ---
            revenue = get_col(inc, ['营业收入', '一、营业收入', '一、营业总收入'])
            operating_cost = get_col(inc, ['营业成本', '二、营业总成本', '营业总成本'])
            net_profit = get_col(inc, ['净利润', '五、净利润', '四、净利润'])
            net_profit_parent = get_col(inc, ['归属于母公司所有者的净利润', '（一）归属于母公司所有者的净利润',
                                               '归属于母公司股东的净利润'])
            operating_profit = get_col(inc, ['营业利润', '三、营业利润'])
            eps = get_col(inc, ['基本每股收益', '（一）基本每股收益'])

            # --- 从资产负债表提取 ---
            total_assets = get_col(bal, ['资产总计', '资产合计'])
            total_liab = get_col(bal, ['负债合计', '负债总计'])
            total_equity = get_col(bal, ['所有者权益合计', '所有者权益（或股东权益）合计',
                                          '股东权益合计', '归属于母公司股东权益合计'])
            current_assets = get_col(bal, ['流动资产合计'])
            current_liab = get_col(bal, ['流动负债合计'])
            inventory = get_col(bal, ['存货'])
            monetary_funds = get_col(bal, ['货币资金'])

            # --- 从现金流量表提取 ---
            operating_cashflow = None
            investing_cashflow = None
            financing_cashflow = None
            if cf is not None:
                operating_cashflow = get_col(cf, ['经营活动产生的现金流量净额'])
                investing_cashflow = get_col(cf, ['投资活动产生的现金流量净额'])
                financing_cashflow = get_col(cf, ['筹资活动产生的现金流量净额'])

            # --- 计算常用财务指标 ---

            # 盈利能力
            grossprofit_margin = None
            if revenue and operating_cost and revenue > 0:
                grossprofit_margin = round((revenue - operating_cost) / revenue * 100, 2)
            netprofit_margin = safe_divide(net_profit, revenue, pct=True)
            op_margin = safe_divide(operating_profit, revenue, pct=True)
            roe = safe_divide(net_profit, total_equity, pct=True)
            roa = safe_divide(net_profit, total_assets, pct=True)

            # 每股指标
            # BPS 需要总股本，这里用 权益/总股本 近似（如果没有直接数据）
            bps = None  # 新浪利润表可能不含BPS，下面尝试用权益/股本计算

            # 偿债能力
            debt_to_assets = safe_divide(total_liab, total_assets, pct=True)
            current_ratio = safe_divide(current_assets, current_liab)
            quick_ratio = None
            if current_assets and inventory and current_liab:
                quick_ratio = safe_divide(current_assets - inventory, current_liab)

            # 营运能力
            assets_turn = safe_divide(revenue, total_assets)

            # 现金流
            ocf_to_revenue = safe_divide(operating_cashflow, revenue, pct=True)
            ocf_to_profit = safe_divide(operating_cashflow, net_profit)

            record = {
                'end_date': period,
                # 每股指标
                'eps': eps,
                # 盈利能力
                'roe': roe,
                'roa': roa,
                'grossprofit_margin': grossprofit_margin,
                'netprofit_margin': netprofit_margin,
                'op_margin': op_margin,
                # 偿债能力
                'debt_to_assets': debt_to_assets,
                'current_ratio': current_ratio,
                'quick_ratio': quick_ratio,
                # 营运能力
                'assets_turn': assets_turn,
                # 现金流
                'operating_cashflow': operating_cashflow,
                'investing_cashflow': investing_cashflow,
                'financing_cashflow': financing_cashflow,
                'ocf_to_revenue': ocf_to_revenue,
                'ocf_to_profit': ocf_to_profit,
                # 规模（原始数据，单位：元）
                'revenue': revenue,
                'net_profit': net_profit,
                'total_assets': total_assets,
                'total_liab': total_liab,
                'total_equity': total_equity,
                'monetary_funds': monetary_funds,
            }
            records.append(record)

        if not records:
            print("错误：未提取到任何财务指标")
            return None

        result_df = pd.DataFrame(records)
        result_df = result_df.sort_values('end_date').reset_index(drop=True)

        print(f"成功提取 {len(result_df)} 期财务数据")

        # ============================================================
        # 步骤5：保存到CSV
        # ============================================================
        print("\n步骤5：保存数据到CSV文件...")
        os.makedirs(DATA_DIR, exist_ok=True)
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE_FULL.replace(".", "_")}_fina_akshare.csv')
        result_df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"数据已保存至：{output_file}")

        # ============================================================
        # 步骤6：数据预览
        # ============================================================
        print("\n" + "=" * 60)
        print("数据预览（最近3期）：")
        print("=" * 60)
        recent = result_df.tail(3).set_index('end_date').T
        print(recent.to_string())

        # 关键指标最新值
        print("\n" + "=" * 60)
        print("关键指标最新值：")
        print("=" * 60)
        latest = result_df.iloc[-1]
        key_fields = [
            ('end_date', '报告期'),
            ('eps', '基本每股收益'),
            ('roe', '净资产收益率(%)'),
            ('roa', '总资产报酬率(%)'),
            ('grossprofit_margin', '销售毛利率(%)'),
            ('netprofit_margin', '销售净利率(%)'),
            ('debt_to_assets', '资产负债率(%)'),
            ('current_ratio', '流动比率'),
            ('quick_ratio', '速动比率'),
            ('assets_turn', '总资产周转率'),
            ('revenue', '营业收入'),
            ('net_profit', '净利润'),
            ('operating_cashflow', '经营活动现金流净额'),
            ('ocf_to_profit', '现金流/净利润'),
        ]
        for field, label in key_fields:
            val = latest.get(field, '-')
            print(f"  {label:<18s}  {val}")

        # 数据列清单
        print(f"\n数据列（共 {len(result_df.columns)} 个字段）：")
        for i, col in enumerate(result_df.columns):
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
