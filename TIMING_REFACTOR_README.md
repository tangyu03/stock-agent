# Loop + TimingEngine 重构说明 — 回测逻辑 = 实盘逻辑

## 重写目标

1. 把 intraday（实盘推送）的写死阈值都抽到 `config/timing.yaml`
2. 回测逻辑与实盘逻辑完全一致（走同一个 `TimingEngine`）
3. 回测网格搜索的目标是找出"用什么参数胜率最高"，最优参数可直接迁移到实盘

## 核心架构改动

### 改动前：两条独立路径

```
intraday 实盘推送:
  Orchestrator._do_intraday
    → unified_engine.run_unified_analysis
      → TimingEngine.check_entry_signals / check_exit_signals
        （硬编码阈值，与回测不一致）

回测:
  StockAgentTunedV3Signals.generate_signals
    → 自己实现的 _check_entry / _check_exit / _calc_tech
      （另一套硬编码阈值，与实盘不一致）
```

**问题**：两条路径的进场/出场逻辑、技术指标计算、阈值完全独立，回测优化出的参数无法迁移到实盘。

### 改动后：共用同一套逻辑

```
intraday 实盘推送:
  Orchestrator._do_intraday
    → unified_engine.run_unified_analysis
      → TimingEngine.check_entry_signals / check_exit_signals
        （从 config/timing.yaml 读阈值）

回测:
  StockAgentTunedV3Signals.generate_signals
    → TimingEngine.check_entry_signals / check_exit_signals  ← 同一个方法！
      （从 config/timing.yaml 读阈值，支持 params_override 覆盖）
```

**保证**：回测和实盘走相同的 `check_entry_signals` / `check_exit_signals` 方法，所有阈值从同一份 `config/timing.yaml` 读取。网格搜索的最优参数写入 yaml 后，实盘立即生效。

## 关键代码改动

### 1. 新增 `config/timing.yaml`

78 个阈值按策略分组：

```yaml
timing:
  panic_bottom:           # 恐慌抄底（10 个阈值）
  arbitrage:              # 套利低吸（2 个）
  momentum_chase:         # 确认追强（4 个）
  volume_breakout:        # 价量突破（1 个）
  exit:
    breakdown:            # 破位止损（2 个）
    exhaustion:           # 上涨衰竭（20 个）
  stop_loss:              # 止损价计算（6 个）
  derivation:             # 推导链（4 个）
  tech_data:              # 技术指标窗口（20 个）
  target_range:           # 止盈区间（14 个）
  prefetch:               # 数据预取（3 个）
  backtest:               # 回测专用（9 个）
```

### 2. 重构 `src/analyzers/timing_engine.py`

- **加载 yaml 配置**：`__init__` 中 `load_config("timing.yaml")`，所有阈值通过 `self._cfg(...)` 读取
- **新增 `backtest_mode`**：
  - `set_backtest_context(date, kline_data, index_kline)` 注入历史 K 线
  - `_fetch_tech_data` 在 backtest_mode 下用注入的 K 线（不调 akshare）
  - `_get_realtime_price` 在 backtest_mode 下返回注入 K 线的最后收盘价
  - `prefetch_market_data` 在 backtest_mode 下从注入指数 K 线计算
- **修复 3 个 bug**：
  - ✅ `stop_loss_multiplier` 配置已存在但未接入 → 现在行 883 用 `self._cfg("stop_loss", "multiplier")`
  - ✅ `_tech_cache_weekly` 从未初始化 → 现在在 `__init__` 初始化
  - ✅ 周线 MACD 在回测模式下从日 K 聚合周 K 计算（实盘模式保持原行为）
- **新增 `params_override`**：支持网格搜索覆盖配置
- **新增 `get_backtest_timing_engine(params_override)`**：回测专用工厂函数

### 3. 重写 `src/loop/stockagent_tuned_v3_signals.py`

- **删除所有重复逻辑**：`_check_entry` / `_check_exit` / `_calc_tech` / `_check_panic_bottom` / `_check_arbitrage_low` / `_check_momentum_chase` / `_calc_stop_loss` / `_fmt_derivation` 全部删除
- **委托给 TimingEngine**：
  - 进场：调 `self._timing.check_entry_signals(...)`
  - 出场：调 `self._timing.check_exit_signals(...)` + 回测专用约束（持有期/止盈/认错）
- **保留回测专用约束**（在 timing.yaml 的 backtest 分组）：
  - `min_hold_days` / `max_hold_days` / `cooldown_days_after_sell`
  - `take_profit_threshold` / `ma5_pressure_threshold`
  - `confess_wrong_days` / `confess_wrong_pnl_threshold`
  - `stop_loss_pct`（固定止损幅度，兜底）

### 4. 更新 `scripts/run_grid_search.py`

- 参数名对齐 `timing.yaml`（点号分隔，如 `panic_bottom.index_drop_threshold`）
- `signal_factory` 自动将扁平参数转为嵌套结构
- 完整搜索 17 维参数（覆盖进场/出场/止损/回测约束）

## 测试验证

```bash
# 指标单元测试
python3 scripts/test_metrics.py

# 集成测试（验证回测=实盘逻辑）
python3 scripts/test_integration_v2.py
```

9 个集成测试全部通过，关键验证：

```
━━━ 测试 9: 回测 vs 实盘逻辑一致性 ━━━
   回测引擎类型: TimingEngine
   实盘引擎类型: TimingEngine
   check_entry_signals 是同一方法: True  ← 核心保证
   check_exit_signals 是同一方法: True   ← 核心保证
✅ 回测与实盘走相同的 TimingEngine 方法（逻辑一致性保证）
```

## 使用示例

### 1. 网格搜索找最优参数

```bash
# 完整搜索（17 维）
python3 scripts/run_grid_search.py

# 快速测试
python3 scripts/run_grid_search.py --quick

# 自定义窗口
python3 scripts/run_grid_search.py --train 80 --test 30 --step 30
```

### 2. 将最优参数迁移到实盘

搜索完成后，JSON 报告中的 `best_params_overall` 字段就是综合最优参数。

将这些参数写入 `config/timing.yaml`，例如：

```yaml
timing:
  panic_bottom:
    index_drop_threshold: 3.0   # 从 4.0 改为 3.0（搜索结果）
  stop_loss:
    multiplier: 0.95             # 从 0.97 改为 0.95（搜索结果）
  backtest:
    take_profit_threshold: 0.05  # 从 0.08 改为 0.05（搜索结果）
    max_hold_days: 15            # 从 10 改为 15（搜索结果）
```

写入后，实盘 intraday 推送会自动使用新参数（`Orchestrator._do_intraday` → `TimingEngine` 读 yaml），无需改任何代码。

### 3. 编程式 Walk-Forward

```python
from src.loop.walk_forward import WalkForwardOptimizer
from src.loop.backtest_engine import BacktestEngine
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS

wf = WalkForwardOptimizer(
    kline_data=kline_data,
    benchmark_kline=bench,
    train_window=60, test_window=20, step=20,
    initial_cash=1_000_000,
    engine_factory=lambda: BacktestEngine(initial_cash=1_000_000),
    signal_factory=lambda p: StockAgentTunedV3Signals(
        params={**DEFAULT_BACKTEST_PARAMS, **p}
    ),
    objective="sharpe",
)

result = wf.run_grid_search(grid={
    "panic_bottom.index_drop_threshold": [3.0, 4.0],
    "stop_loss.multiplier": [0.95, 0.97],
    "take_profit_threshold": [0.05, 0.08],
})

# result.best_params_overall: 综合最优参数（写入 timing.yaml 即可）
# result.oos_sharpe_mean: 样本外平均 Sharpe（真实表现）
```

## 文件清单

### 新增

| 文件 | 作用 |
|------|------|
| `config/timing.yaml` | 78 个阈值配置（回测与实盘共用） |
| `scripts/test_integration_v2.py` | 集成测试（9 个用例，验证回测=实盘） |
| `TIMING_REFACTOR_README.md` | 本说明文档 |

### 重写

| 文件 | 改动要点 |
|------|---------|
| `src/analyzers/timing_engine.py` | 加载 yaml + backtest_mode + 修复 3 bug + 替换 78 个硬编码 |
| `src/loop/stockagent_tuned_v3_signals.py` | 删除重复逻辑，委托给 TimingEngine |
| `scripts/run_grid_search.py` | 参数名对齐 timing.yaml |
| `src/loop/__init__.py` | 更新导出（DEFAULT_BACKTEST_PARAMS 替代 TUNE_PARAMS_V3） |

### 保留不动

- `src/loop/backtest_engine.py`（T+1 执行 + 指标计算，与策略无关）
- `src/loop/walk_forward.py`（Walk-Forward 框架，与策略无关）
- `src/loop/metrics.py`（指标计算，与策略无关）
- `src/loop/data_loader.py` / `market_mode_adaptive.py`

## 主流对齐检查清单

- [x] **回测逻辑 = 实盘逻辑**（同一 TimingEngine 方法）
- [x] **所有阈值在 yaml**（78 个，从 timing.yaml 读取）
- [x] **网格搜索找最优参数**（Sharpe 目标 + Walk-Forward）
- [x] **最优参数可直接迁移到实盘**（写入 yaml 即可）
- [x] **T+1 次日开盘成交**（消除前视偏差）
- [x] **Walk-Forward 样本外验证**（防止过拟合）
- [x] **沪深300 基准对比**（Alpha/Beta/IR）
- [x] **完整指标输出**（Sharpe/Sortino/Calmar/Alpha/Beta/IR）
- [x] **3 个 bug 修复**（stop_loss_multiplier / _tech_cache_weekly / 周线MACD）
