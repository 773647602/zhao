# 弱转强开盘买入窗口任务（9:29-9:35 每 15 秒 tick 监控自动买入）

## Context

用户需求：弱转强策略选出的股票自动加入监控池后，需要**开盘买入窗口**专责监控：

- 每 15 秒获取一次 tick 并判定买入条件
- 符合买入条件 → 自动下单
- 循环时间 9:29 - 9:35；9:35 后未满足买入规则即放弃（当天不再买入）

现状：现有 LiveTradingLoop 由 scheduler 09:00 启动、14:55 停止，每 60 秒循环评估**所有**启用策略的候选+持仓（LiveSimRunner worker，[live_simulator.py#L504-L522](file:///d:/CASE-AI量化系统/lib/live_simulator.py#L504-L522)），周期与窗口都不满足需求。且弱转强买入判定（strat_weak_to_strong）目前全天可触发，9:35 后 hook/回钩条件满足仍会买入，违背"放弃买入"意图。

## 方案

新增独立的**开盘买入窗口任务**（后台线程，与 60s 主循环并行），由 scheduler 在 09:29 启动，任务内部到 9:35 自动停止；同时在策略买入判定中加时间窗口限制，保证 9:35 后主循环也不会再买入弱转强。

### 1. 新建 `live_trading/open_buy_window.py`

`OpenBuyWindowRunner`（参考 LiveSimRunner 的 worker 模式实现）：

- 常量：`WINDOW_START=(9,29)`、`WINDOW_END=(9,35)`、`CYCLE_SECONDS=15`
- `start()`：启动后台 daemon 线程；内部持有：
  - `MarketDataProvider`（复用 [live_loop.py#L58](file:///d:/CASE-AI量化系统/live_trading/live_loop.py#L58) 的类，get_latest_tick 走 BigQMT 桥）
  - `LiveTradingLoop` 实例（同一 `OUTPUTS_LIVE_STATE` state 文件），复用其 `_handle_signal` 下单链路（风控+trader.buy+apply_fill+increment_position+写 signals/orders）与 `AlertRouter`
  - `capital` 从 `load_mock_config()` 读取（与 LiveSimRunner 一致）
- `stop()` / `running()` / `status()`：与 LiveSimRunner 同款
- `_worker()`：
  - 每轮先检查时间：非工作日或 ≥ 9:35 → 自动 `stop()` 退出
  - `_run_cycle()` → 按 CYCLE_SECONDS 间隔循环（逐秒检查 stop_flag，同 LiveSimRunner worker 模式）
- `_run_cycle()`：
  1. 候选 = `query_selection_pool(strategy="weak_to_strong", trade_date=今日, trigger="cron")`（[selection_store.py#L159](file:///d:/CASE-AI量化系统/lib/selection_store.py#L159)；弱转强 09:26 定时选股已写入选股池并并入监控池）
  2. 空候选 → 本轮跳过
  3. 每只候选：取 `get_strategy("weak_to_strong").evaluator`（即 `strat_weak_to_strong`），调用 `evaluator(code, market, capital)`
  4. 信号 `side == "buy"` → `self._loop._handle_signal(code, signal)`（复用现有风控+真实下单；下单后 DB `trade_strategy_position` 落库，下一轮评估自动转为持仓分支，天然防重复下单）
  5. 信号 hold/sell → 跳过
- 异常处理：单只失败跳过不中断整轮；记录日志

防重复设计：买入判定依赖 DB 持仓（`query_strategy_positions`，[strategy_registry.py#L1192](file:///d:/CASE-AI量化系统/lib/strategy_registry.py#L1192)），窗口任务与 60s 主循环都以"无持仓才买"为前提，下单即落库，双方并行不会重复买入同一只。

### 2. `lib/strategy_registry.py`：`strat_weak_to_strong` 加买入时间窗口

在"无持仓 → 买入判断"分支（[strategy_registry.py#L1218](file:///d:/CASE-AI量化系统/lib/strategy_registry.py#L1218)）开头插入：

```python
# 买入时间窗口: 仅 09:29-09:35 允许买入 (开盘窗口), 窗口外/窗口结束后放弃当天买入
from datetime import time as _dtt, datetime as _dtn
_t = _dtn.now().time()
if not (_dtt(9, 29) <= _t <= _dtt(9, 35)):
    return _hold("weak_to_strong", "非买入窗口 (仅 09:29-09:35), 放弃当日买入")
```

- 卖出分支（持仓）不受窗口限制（止盈止损全天有效）
- 这样主循环 9:35 后对弱转强只返回 hold，满足"9:35 后放弃买入"

### 3. `scheduler.py`：注册 09:29 启动 job

新增 `job_open_buy_window_start()`：
- 创建 `OpenBuyWindowRunner()`，已在运行则跳过，否则 `start()`
- 用 venv Python 启动（与现有 job 一致，scheduler.py 由 venv python 运行）
- 注册 cron：`hour=9, minute=29, day_of_week="mon-fri"`（timezone Asia/Shanghai）
- 无需单独 09:35 stop job（任务内部到点自停；残留时下次 start 会先 stop）

## 关键复用点

- `MarketDataProvider`（[live_loop.py#L58](file:///d:/CASE-AI量化系统/live_trading/live_loop.py#L58)）：tick 获取（BigQMT 桥→公网→xtdata 多级回退）
- `LiveTradingLoop._handle_signal`（[live_loop.py#L545](file:///d:/CASE-AI量化系统/live_trading/live_loop.py#L545) 附近）：风控+下单+记账全链路
- `query_selection_pool`（[selection_store.py#L159](file:///d:/CASE-AI量化系统/lib/selection_store.py#L159)）：候选来源
- `strat_weak_to_strong`（[strategy_registry.py#L1162](file:///d:/CASE-AI量化系统/lib/strategy_registry.py#L1162)）：买入条件判定（含 days=0 当日买入、开盘涨幅上下限、open/hook/dip_recover 模式）
- LiveSimRunner worker 模式（[live_simulator.py#L504-L526](file:///d:/CASE-AI量化系统/lib/live_simulator.py#L504-L526)）：线程启停模板

## 验证

1. Python 语法检查三个改动文件
2. 重启 scheduler 进程，确认日志出现 `[REG] 09:29 开盘买入窗口`
3. 手动验证窗口逻辑（非窗口时间）：
   - 调用 `strat_weak_to_strong` 在 9:35 后 → 返回 hold"非买入窗口"
   - `OpenBuyWindowRunner._run_cycle()` 在非窗口时间空转不下单
4. 端到端（可选）：临时把 WINDOW 常量调宽到当前时间附近，启动 runner，用一条弱转强候选观察 tick 获取日志与判定结果
5. 页面回归：弱转强配置弹窗仍正常；策略详情/信号不受影响
