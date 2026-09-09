# -*- coding: utf-8 -*-
"""
财务数据下载脚本 - QMT / miniQMT 版
使用xtquant下载贵州茅台(600519.SH)的综合财务数据
从资产负债表、利润表、现金流量表、每股指标四张报表中提取常用财务指标

保存文件（均在 data/ 目录下）：
  - 600519_SH_fina_QMT.csv  -- 综合财务指标（按报告期，多期数据）

运行：python 财务数据-QMT.py
环境：需安装QMT并配置好xtquant
"""
import os
import time
import traceback
from datetime import datetime
import pandas as pd
from xtquant import xtdata


# ============================================================
# 配置
# ============================================================
STOCK_CODE = '600519.SH'   # 贵州茅台（QMT格式）
STOCK_NAME = '贵州茅台'
DATA_START = '20150101'     # 财务数据起始日期（尽量多取历史数据）
DATA_END = '20261231'       # 财务数据截止日期

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 需要获取的财务报表
TABLE_LIST = ['Balance', 'Income', 'CashFlow', 'PershareIndex', 'Capital']


def normalize_timetag(ts_val):
    """
    将xtquant的m_timetag转换为日期字符串（如 20241231）。
    xtquant返回的m_timetag可能是字符串（如 '20200331'）或数值型时间戳。
    """
    if ts_val is None:
        return None
    # 如果已经是日期字符串格式
    s = str(ts_val).strip()
    if len(s) == 8 and s.isdigit():
        return s
    # 如果是数值型时间戳（毫秒或秒）
    try:
        v = float(s)
        if v == 0:
            return None
        if v > 1e12:
            v = v / 1000
        return datetime.fromtimestamp(v).strftime('%Y%m%d')
    except (OSError, ValueError, TypeError):
        return None


def get_field(record, field_names, default=None):
    """
    从记录字典中获取字段值，支持多个候选字段名。
    因xtquant不同版本的字段名可能不同，按优先级依次尝试。
    """
    for name in field_names:
        val = record.get(name)
        if val is not None:
            return val
    return default


def safe_divide(a, b, pct=False):
    """安全除法，b为0时返回None。pct=True时结果乘以100。"""
    if a is None or b is None:
        return None
    a, b = float(a), float(b)
    if b == 0:
        return None
    result = a / b
    if pct:
        result *= 100
    return round(result, 4)


def build_period_map(data_list):
    """
    将xtquant返回的财务数据列表转换为 {报告期日期: 记录字典} 的映射。
    兼容DataFrame和list两种返回格式。
    """
    period_map = {}
    if isinstance(data_list, pd.DataFrame):
        for _, row in data_list.iterrows():
            period_date = normalize_timetag(row.get('m_timetag'))
            if period_date:
                period_map[period_date] = row.to_dict()
    elif isinstance(data_list, list):
        for rec in data_list:
            if isinstance(rec, dict):
                period_date = normalize_timetag(rec.get('m_timetag'))
                if period_date:
                    period_map[period_date] = rec
    return period_map


def extract_all_periods(data):
    """
    从xtquant返回的单只股票财务数据中，提取所有报告期的综合财务指标。

    参数：
      data: xtdata.get_financial_data 返回的 {stock: {table: data}} 中该股票的部分

    返回：按报告期排列的记录列表
    """
    stock_data = data.get(STOCK_CODE, {})
    if not stock_data:
        return []

    # 将各报表数据按报告期索引
    pershare_map = build_period_map(stock_data.get('PershareIndex', []))
    balance_map = build_period_map(stock_data.get('Balance', []))
    income_map = build_period_map(stock_data.get('Income', []))
    cashflow_map = build_period_map(stock_data.get('CashFlow', []))
    capital_map = build_period_map(stock_data.get('Capital', []))

    # 获取所有出现过的报告期
    all_periods = sorted(set(
        list(pershare_map.keys()) +
        list(balance_map.keys()) +
        list(income_map.keys()) +
        list(cashflow_map.keys())
    ))

    print(f"  发现 {len(all_periods)} 个报告期")
    if all_periods:
        print(f"  范围：{all_periods[0]} 至 {all_periods[-1]}")

    # 打印首个报告期的可用字段名（便于调试和了解数据结构）
    if all_periods:
        first_period = all_periods[-1]  # 最新一期
        print(f"\n  [字段参考] 最新报告期 {first_period} 各报表可用字段：")
        for table_name, table_map in [
            ('PershareIndex', pershare_map),
            ('Balance', balance_map),
            ('Income', income_map),
            ('CashFlow', cashflow_map),
        ]:
            rec = table_map.get(first_period, {})
            fields = [k for k in rec.keys() if not k.startswith('m_')]
            if fields:
                print(f"    {table_name}: {', '.join(fields[:15])}{'...' if len(fields) > 15 else ''}")

    records = []
    for period in all_periods:
        ps = pershare_map.get(period, {})
        bal = balance_map.get(period, {})
        inc = income_map.get(period, {})
        cf = cashflow_map.get(period, {})
        cap = capital_map.get(period, {})

        # ---- 每股指标（PershareIndex表）----
        eps = get_field(ps, ['s_fa_eps_basic'])
        bps = get_field(ps, ['s_fa_bps'])
        ocfps = get_field(ps, ['s_fa_ocfps'])
        undist_ps = get_field(ps, ['s_fa_undistributedps'])

        # ---- 盈利能力（PershareIndex表中有预计算的比率）----
        roe = get_field(ps, ['du_return_on_equity', 'equity_roe', 'net_roe'])
        grossprofit_margin_ps = get_field(ps, ['sales_gross_profit'])  # 毛利率（百分比值）

        # 从利润表取绝对值
        revenue = get_field(inc, ['revenue', 'operating_revenue', 'revenue_inc'])
        net_profit = get_field(inc, ['net_profit_incl_min_int_inc', 'net_profit_excl_min_int_inc'])
        operating_profit = get_field(inc, ['oper_profit'])
        operating_cost = get_field(inc, ['cost_of_goods_sold', 'total_operating_cost', 'total_expense'])

        # 计算盈利指标
        grossprofit_margin = grossprofit_margin_ps  # 优先使用PershareIndex中的毛利率
        if grossprofit_margin is None and revenue and operating_cost:
            r, c = float(revenue), float(operating_cost)
            if r > 0:
                grossprofit_margin = round((r - c) / r * 100, 2)

        netprofit_margin = safe_divide(net_profit, revenue, pct=True)
        op_margin = safe_divide(operating_profit, revenue, pct=True)

        # ---- 偿债能力（Balance表）----
        total_assets = get_field(bal, ['tot_assets'])
        total_liab = get_field(bal, ['tot_liab'])
        total_equity = get_field(bal, ['total_equity', 'tot_shrhldr_eqy_incl_min_int'])
        current_assets = get_field(bal, ['total_current_assets'])
        current_liab = get_field(bal, ['total_current_liability'])
        inventory = get_field(bal, ['inventories'])
        monetary_funds = get_field(bal, ['cash_equivalents'])

        debt_to_assets = safe_divide(total_liab, total_assets, pct=True)
        current_ratio = safe_divide(current_assets, current_liab)
        quick_ratio = None
        if current_assets and inventory and current_liab:
            quick_ratio = safe_divide(float(current_assets) - float(inventory), current_liab)

        # ROA 从利润表和资产负债表计算
        roa = safe_divide(net_profit, total_assets, pct=True)
        # 如果PershareIndex中ROE为空，从报表计算
        if roe is None and net_profit and total_equity:
            roe = safe_divide(net_profit, total_equity, pct=True)

        # ---- 营运能力 ----
        assets_turn = safe_divide(revenue, total_assets)

        # ---- 现金流（CashFlow表）----
        operating_cashflow = get_field(cf, ['net_cash_flows_oper_act'])
        investing_cashflow = get_field(cf, ['net_cash_flows_inv_act'])
        financing_cashflow = get_field(cf, ['net_cash_flows_fnc_act'])

        ocf_to_revenue = safe_divide(operating_cashflow, revenue, pct=True)
        ocf_to_profit = safe_divide(operating_cashflow, net_profit)

        # ---- 总股本（Capital表）----
        total_shares = get_field(cap, ['totalShares', 'totalCapital', 'total_shares'])

        record = {
            'end_date': period,
            # 每股指标
            'eps': eps,
            'bps': bps,
            'ocfps': ocfps,
            'undist_profit_ps': undist_ps,
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
            # 规模
            'total_assets': total_assets,
            'total_liab': total_liab,
            'total_equity': total_equity,
            'revenue': revenue,
            'net_profit': net_profit,
            'monetary_funds': monetary_funds,
            'total_shares': total_shares,
        }
        records.append(record)

    return records


def download_financial_data():
    """
    下载贵州茅台的综合财务数据
    """
    print(f"开始下载财务数据")
    print(f"股票：{STOCK_NAME}({STOCK_CODE})")
    print(f"日期范围：{DATA_START} 至 {DATA_END}")
    print("-" * 60)

    try:
        # 步骤0：连接QMT服务（miniQMT需先启动客户端）
        print("步骤0：连接QMT数据服务...")
        connect_result = xtdata.connect()
        print(f"连接结果：{connect_result}")

        # 步骤1：下载财务数据到本地缓存
        # 注意：download_financial_data（同步版）会卡住，改用 download_financial_data2（异步版）
        print("\n步骤1：下载财务数据到本地缓存...")
        done_count = [0]
        total_tables = len(TABLE_LIST)

        def on_download_done(data):
            done_count[0] += 1
            print(f"  已下载 {done_count[0]}/{total_tables} 张报表", flush=True)

        for table_name in TABLE_LIST:
            xtdata.download_financial_data2(
                stock_list=[STOCK_CODE],
                table_list=[table_name],
                start_time=DATA_START,
                end_time=DATA_END,
                callback=on_download_done
            )

        # 等待所有报表下载完成（最多等待60秒）
        for _ in range(60):
            if done_count[0] >= total_tables:
                break
            time.sleep(1)

        if done_count[0] < total_tables:
            print(f"警告：仅下载了 {done_count[0]}/{total_tables} 张报表，继续尝试获取数据...")
        else:
            print("全部下载完成")

        time.sleep(1)

        # 步骤2：获取并解析财务数据
        print("\n步骤2：获取并解析财务数据...")
        data = xtdata.get_financial_data(
            stock_list=[STOCK_CODE],
            table_list=TABLE_LIST,
            start_time=DATA_START,
            end_time=DATA_END,
            report_type='report_time'
        )

        if not data or STOCK_CODE not in data:
            print("错误：无法获取财务数据")
            return None

        # 提取所有报告期的财务指标
        records = extract_all_periods(data)

        if not records:
            print("错误：未提取到任何财务指标")
            return None

        df = pd.DataFrame(records)
        df = df.sort_values('end_date').reset_index(drop=True)

        print(f"\n成功提取 {len(df)} 期财务数据")

        # 步骤3：保存到CSV
        print("\n步骤3：保存数据到CSV文件...")
        os.makedirs(DATA_DIR, exist_ok=True)
        output_file = os.path.join(DATA_DIR, f'{STOCK_CODE.replace(".", "_")}_fina_QMT.csv')
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"数据已保存至：{output_file}")

        # 步骤4：数据预览
        print("\n" + "=" * 60)
        print("数据预览（最近3期）：")
        print("=" * 60)
        recent = df.tail(3).set_index('end_date').T
        print(recent.to_string())

        # 关键指标最新值
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
            ("速动比率",         latest.get('quick_ratio')),
            ("总资产周转率",     latest.get('assets_turn')),
            ("每股经营现金流",   latest.get('ocfps')),
            ("营业收入",         latest.get('revenue')),
            ("净利润",           latest.get('net_profit')),
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
