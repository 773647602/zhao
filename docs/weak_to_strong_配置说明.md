# 弱转强（weak_to_strong）策略配置说明

> 配置文件：`config/strategy_selection.yaml`
> 修改生效：保存后调用 `POST /api/strategy/run` 立即选股，或等待定时选股（`schedule`）自动执行；买入/卖出条件每次实时读取，**保存即生效，无需重启**。

---

## 1. 策略概述

弱转强：**昨日弱（收跌/缩量）→ 今日高开放量转强**的强势票筛选，入选后按可配置的买入/卖出条件自动跟踪买卖。

完整流程分三步：

```
全市场日K面板（有数据的A股）
   │  ① 选股（selection）：声明式 filters 过滤 + 排序 + top_n
   ▼
选股池 trade_selection_pool（只记录候选，不产生信号）
   │  ② 触发：自动选股（cron）→ 自动并入监控池；手动选股（manual）→ 不入池
   ▼
实盘引擎逐股评估（trade）
   │  ③ 无持仓 → 按 buy 规则判断是否买入（需实时 tick）
   │   有持仓 → 按 sell 规则判断止盈/止损
   ▼
买入/卖出信号 → 实盘真实下单（bigqmt 桥）
```

---

## 2. 配置结构总览

```yaml
strategies:
- name: weak_to_strong        # 策略标识（固定）
  label: 弱转强               # 页面显示名
  enabled: true               # 策略总开关
  schedule: 09:26             # 定时选股时间
  top_n: 10                   # 每轮最多入选只数
  selection:                  # ① 选股配置
    mode: generic_filter
    sort_by: amount
    sort_desc: true
    filters:
      max_prev_pct: 0         # 昨日收盘涨幅 < 0%（弱）
      min_open_pct: 3         # 今日开盘涨幅 > 3%（转强）
      min_vol_grow_pct: 5     # 昨量较前日增幅 >= 5%
      ma_period: 10           # 昨日收盘站上 10 日线
      max_index_pct: null     # 大盘开盘涨幅闸门（禁用）
  trade:                      # ②③ 买卖配置
    enabled: true             # 自动买卖开关（false=仅选股）
    buy:
      days: 1                 # 入选后第 N 个交易日可买（1=次日）
      mode: hook              # 买入模式：open / dip_recover / hook
      open_pct_min: null      # 开盘涨幅门槛（仅 open 模式用）
      dip_pct: 3              # 下杀幅度 %（dip_recover）
      recover_pct: 0.2        # 上钩回升 %（dip_recover）
      plunge_pct: 2.5         # 下杀确认 %（hook）
      rebound_pct: 0.5        # 回钩触发 %（hook）
    sell:
      take_profit_pct: 5      # 浮盈 >= 5% 止盈
      stop_loss_pct: -3       # 浮亏 <= -3% 止损
```

---

## 3. 选股配置（selection）

### 3.1 通用项

| 字段 | 含义 | 当前值 |
|---|---|---|
| `mode` | 选股方式，固定 `generic_filter`（声明式过滤） | `generic_filter` |
| `sort_by` | 排序字段：`amount`（成交额）/`score`/`pct_change` 等 | `amount` |
| `sort_desc` | 是否降序（成交额越大越靠前） | `true` |
| `top_n` | 每轮最多入选只数；`0`=不限 | `10` |
| `schedule` | 定时选股时间（HH:MM），由 scheduler 触发 | `09:26` |
| `enabled` | 策略总开关 | `true` |

### 3.2 核心过滤条件（filters，弱转强四要素）

> 所有阈值均为**百分比数值**；设为 `null`（或留空）= 禁用该项。

| 字段 | 含义 | 判断 | 当前值 |
|---|---|---|---|
| `max_prev_pct` | 昨日收盘涨幅上限（**弱**） | 昨日涨幅 `< 阈值` | `0` |
| `min_open_pct` | 今日开盘涨幅下限（**转强**） | 今开较昨收涨幅 `> 阈值` | `3` |
| `min_vol_grow_pct` | 昨量较前日量能增幅（**放量**） | 昨量/前日量 `>= 1+阈值%` | `5` |
| `ma_period` | 昨日收盘需站上的均线周期（**强势确认**） | 昨收 `>=` N日均线；`null`=禁用 | `10` |
| `max_index_pct` | 大盘（上证）开盘涨幅闸门 | 大盘开盘涨幅 `> 阈值` 时**全体不选**；`null`=禁用 | `null` |

### 3.3 可选过滤条件（filters 均可加，null/缺省=禁用）

| 字段 | 含义 |
|---|---|
| `index_ma_period` | 大盘昨日收盘需站上的均线周期（未站上则不选票） |
| `min_price` / `max_price` | 现价区间（元） |
| `min_change_pct` / `max_change_pct` | 今日涨跌幅区间（%） |
| `min_turnover` / `max_turnover` | 换手率区间（%） |
| `min_market_cap` / `max_market_cap` | 总市值区间（亿元） |
| `min_vol_ratio` | 量比下限 |
| `excluded_codes` | 排除代码列表，如 `["600519.SH"]` |

---

## 4. 买入配置（trade.buy）

### 4.1 通用项

| 字段 | 含义 | 当前值 |
|---|---|---|
| `days` | 入选日**之后**经过的交易日数 ≥ N 才可买（1=次日开盘起可买） | `1` |
| `open_pct_min` | 当日开盘涨幅下限（`null`=不限；仅 `open` 模式生效） | `null` |
| `mode` | 买入触发模式（见下） | `hook` |

### 4.2 买入模式（mode）

**① `open` 开盘买**（默认）
- 条件：`days` 已满 + 当日开盘涨幅 `>= open_pct_min`（若设置）
- 数据：日K（开盘价/昨收）

**② `dip_recover` 开盘下杀后上钩买**
- 条件：开盘后最低价较开盘价跌幅 `>= dip_pct`（下杀）**且** 当前价较开盘价回升 `>= recover_pct`（上钩）
- 当前价以**实时 tick** 为准；`tick 缺失 → 放弃买入`
- 数据：日K + 当日分钟K（最低点）+ 实时 tick

| 字段 | 含义 | 当前值 |
|---|---|---|
| `dip_pct` | 下杀幅度：盘中最低较开盘跌幅 ≥ 该 % | `3` |
| `recover_pct` | 上钩回升：现价较开盘回升 ≥ 该 % | `0.2` |

**③ `hook` 开盘下杀后回钩买（OpenHookHunter，跟踪新低）**
- 状态机：
  1. **下杀确认**：现价 `<=` 开盘价 × (1 − plunge_pct) → armed
  2. **跟踪新低**：逐分钟 `low = min(low, bar.low)`（低点可继续下移）
  3. **回钩触发**：现价 `>=` 最低点 × (1 + rebound_pct) → 市价买入（一次即停）
- **触发必须以实时 tick 价为当前价**：分钟K仅预热状态（下杀/低点），`tick 缺失 → 放弃买入`
- 数据：日K + 当日分钟K（预热状态机）+ 实时 tick（触发）

| 字段 | 含义 | 当前值 |
|---|---|---|
| `plunge_pct` | 下杀确认：现价较开盘价跌幅 ≥ 该 % | `2.5` |
| `rebound_pct` | 回钩触发：现价自盘中最低点回升 ≥ 该 % | `0.5` |

> ⚠️ 三种模式（除 `open`）都依赖**实时 tick**——盘中行情中断/休市拿不到 tick 时会输出 `无实时tick价, 放弃买入`，不会基于过时分钟K价下单。

### 4.3 买入前置条件（所有模式通用）
1. 该股在 `trade_selection_pool` 弱转强候选池中（且 `source_type=cron` 自动选股才入监控池）
2. 入选日之后经过的交易日数 ≥ `days`
3. 当前无该股持仓（买入后不再重复买）

---

## 5. 卖出配置（trade.sell）

有持仓时按浮盈亏判断，**优先于买入判断**：

| 字段 | 含义 | 当前值 |
|---|---|---|
| `take_profit_pct` | 浮盈 ≥ 该 % → 卖出（止盈）；`null`=禁用 | `5` |
| `stop_loss_pct` | 浮亏 ≤ 该 % → 卖出（止损，负值）；`null`=禁用 | `-3` |

- 现价取**最新日K收盘**（当日盘中日K实时更新）
- 同时命中止盈/止损线时，按代码顺序**先判断止盈**再判断止损
- 未命中时输出 `持仓浮盈 x.xx% (止盈 5% / 止损 -3%)`，继续持有

---

## 6. trade.enabled 与监控池联动

| 配置 | 行为 |
|---|---|
| `trade.enabled: true` | 自动选股后：候选写入选股池**并自动并入监控池**（`watch_pool.yaml`），引擎对监控标的逐股评估买卖，满足买入规则 → **真实下单（无需审批）** |
| `trade.enabled: false` | 仅选股、仅记录候选，**不产生买卖信号**（返回 `自动买卖未启用, 仅选股`） |

- 手动"立即选股"（trigger=manual）→ 只写选股池（`source_type=manual`），**不入监控池**
- 监控池（页面"监控池"面板 / 引擎监控标的）= 当日 `source_type=cron` 的选股候选

---

## 7. 数据依赖

| 数据 | 用途 | 来源 |
|---|---|---|
| 日K（open/high/low/close/volume/amount） | 选股四要素、`open` 买入、卖出判断 | `trade_stock_daily`（scheduler 定时增量） |
| 当日分钟K（1m） | `dip_recover` 最低点、`hook` 状态机预热 | `trade_stock_minute` |
| 实时 tick（lastPrice） | `dip_recover`/`hook` 的当前价与触发 | bigqmt / QMT 行情 |
| 上证指数日K | `max_index_pct` / `index_ma_period` 闸门 | `trade_stock_daily`（指数代码） |
| 选股池 / 持仓 | 候选判断、卖出浮盈亏 | `trade_selection_pool` / `trade_strategy_position` |
| 国金账户 | 真实下单、持仓、资金 | MiniQMTTraderV2（bigqmt 桥） |

---

## 8. 当前生效配置（2026-09-09）

```yaml
- name: weak_to_strong
  label: 弱转强
  enabled: true
  schedule: 09:26
  top_n: 10
  selection:
    mode: generic_filter
    sort_by: amount
    sort_desc: true
    filters:
      max_prev_pct: 0        # 昨日收跌
      min_open_pct: 3        # 今日高开 >3%
      min_vol_grow_pct: 5    # 昨量较前日增 ≥5%
      ma_period: 10          # 昨收站上10日线
      max_index_pct: null    # 大盘闸门禁用
  trade:
    enabled: true            # 自动实盘买卖
    buy:
      days: 1                # 入选次日可买
      mode: hook             # 下杀回钩（tick触发）
      open_pct_min: null
      dip_pct: 3
      recover_pct: 0.2
      plunge_pct: 2.5        # 跌2.5%确认下杀
      rebound_pct: 0.5       # 自最低回升0.5%触发买入
    sell:
      take_profit_pct: 5     # 止盈5%
      stop_loss_pct: -3      # 止损-3%
```

---

## 9. 常用调整示例

**提高入选门槛（更严的弱转强）**
```yaml
filters:
  max_prev_pct: -3      # 昨日跌幅 ≥3% 才算"弱"
  min_open_pct: 4       # 今日高开 >4%
  min_vol_grow_pct: 50  # 昨量翻倍以上
  ma_period: 5          # 站上5日线
```

**改为开盘直接买（入选次日开盘涨幅≥1% 就买）**
```yaml
trade:
  buy:
    mode: open
    days: 1
    open_pct_min: 1
```

**改为下杀上钩买（跌3%后回封开盘价上方0.2%）**
```yaml
trade:
  buy:
    mode: dip_recover
    dip_pct: 3
    recover_pct: 0.2
```

**只选股、不自动买卖**
```yaml
trade:
  enabled: false
```
