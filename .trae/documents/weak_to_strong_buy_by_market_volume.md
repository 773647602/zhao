# 弱转强：昨日全市场量能决定买入时点

## Context（背景）

用户希望「弱转强」新增买入条件：根据**昨日全市场量能**决定当天是否买入（单阈值规则）：

* 昨日全市场量能 **＜** 阈值（xxx 万亿）→ **当天不买**，推迟到第 N+1 个交易日

* 昨日全市场量能 **＞** 阈值 → **当天买入**

用户已确认采用\*\*「跟随重新选股」**实现方式：弱转强每个交易日 09:26 都会重新选股、次日 09:00-09:39 开盘窗口再评估。因此当天量能不足时**跳过当天买入\*\*即可，无需跨天记账、无需改数据库表结构；第二天由新的选股 + 新窗口自然决定是否买入。

## 改动点

### 1. 买入门控逻辑 — `live_trading/open_buy_window.py`

在每只候选股**下单前**增加市场量能门控，量能不足则当天不买：

* 新增模块函数 `_prev_market_amount_yi()`：调用 `lib.market_metrics.query_market_volume()`（不传日期，返回倒序，取第一条 `total_amount`，单位元）→ 除以 `1e12` 得「昨日全市场量能（万亿）」。查询失败/无数据返回 `None`。

* 在 `_evaluate_and_order`（[L345-L385](file:///d:/CASE-AI量化系统/live_trading/open_buy_window.py#L345-L385)）中，`result.get("side") == "buy"` 之后、`_place_order` 之前插入：

  * 取当前候选所属策略（`self._candidate_strategies.get(code)`）的 `trade.buy.buy_market_min`（万亿）。未配置（None/''）→ 跳过门控，正常买入。

  * 已配置：若 `prev_amount_yi is None`（昨日数据缺失）**或** `prev_amount_yi < threshold` → 设置 `detail["not_buy_reason"] = "昨日全市场量能不足(不足阈值)，推迟下个交易日"` 并 `return False`（继续监控，不买）。

* 为避免每股每 30 秒重复查库：加一个**按运行日期缓存** `_buy_gate`（记录日期/阈值/昨日量能/是否拦截），在 `start()` 新的一天清空重算。

### 2. 后端保存白名单 — `routes/strategy_page.py`

[\_TRADE\_BUY\_KEYS](file:///d:/CASE-AI量化系统/routes/strategy_page.py#L77-L79) 增加 `"buy_market_min"`，使该字段能通过 `weaktest_config_set → _merge_weak_trade` 写入 yaml。

### 3. 前端表单与读写 — `templates/strategy.html`

* `_weakEditable`（[L496-L524](file:///d:/CASE-AI量化系统/templates/strategy.html#L496-L524)）加载：`out.buy_market_min = (b.buy_market_min !== null && b.buy_market_min !== undefined) ? b.buy_market_min : '';`

* `saveConfig`（[L543-L558](file:///d:/CASE-AI量化系统/templates/strategy.html#L543-L558)）的 `trade.buy` 对象增加：`buy_market_min: this._numOrNull(this.cfg.values.buy_market_min),`

* 买入条件区域（[L170-L174](file:///d:/CASE-AI量化系统/templates/strategy.html#L170-L174) 高开上限示例位置后）新增一个输入框，与现有一致样式：
  `昨日全市场量能 ＜ xxx 万亿则不买入（推迟下个交易日）`，`x-model="cfg.values.buy_market_min"`，`placeholder="留空=禁用"`。

### 4. 默认配置 — `config/strategy_selection.yaml`

弱转强 `trade.buy` 增加 `buy_market_min: null`（默认禁用）。页面保存会自动更新此值。

## 复用的现成能力

* `lib/market_metrics.query_market_volume()`（[L82-L103](file:///d:/CASE-AI量化系统/lib/market_metrics.py#L82-L103)）：返回最新一条 `total_amount`（元），即「昨日/最近收盘日」全市场量能。

* `_TRADE_BUY_KEYS` 白名单机制：已有 14 个买入字段走此持久化，新增字段沿用同一模式。

* 前端 `_weakEditable` / `saveConfig` 的 trade.buy 读写模式、`_numOrNull` 工具。

## 校验（Verification）

1. **配置持久化**：`POST /api/strategy/weaktest/config` 传 `trade.buy.buy_market_min`，确认写回 yaml；GET 能回显。
2. **门控逻辑**（手工/脚本）：构造 `query_market_volume` 返回量能 < 阈值 → `_evaluate_and_order` 返回 `False` 且 `not_buy_reason` 正确；≥阈值或未配置 → 正常运行买入评估。
3. **前端**：刷新策略页（F5）弱转强「买入条件」出现该输入框；保存后 `_TRADE_BUY_KEYS` 生效。
4. **服务重启**：路由白名单改动需重启 app.py；HTML 模板刷新页面即生效。
5. **数据依赖提示**：`trade_market_volume` 目前缺 09-09 数据。若启用该门控而昨日数据缺失，会按「量能不足」保守不买；建议先回填缺失历史数据。

