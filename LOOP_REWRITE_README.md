# Loop 模块重写说明 — 主流量化回测逻辑对齐

## 重写目标

将 `src/loop/` 模块重写为与主流量化回测框架（Zipline/Backtrader/VectorBT/Pyfolio）对齐的版本，支持阈值参数网格搜索和最优结果输出。

## 核心改进对比

### 1. 执行时点：T+1 次日开盘成交

| 维度 | 旧版 | 新版 |
|------|------|------|
| 信号生成 | T 日收盘 | T 日收盘 |
| 实际成交 | T 日收盘价（含滑点） | **T+1 日开盘价（含滑点）** |
| 前视偏差 | 有（轻度） | **无** |
| 涨跌停判断 | T 日数据 | **T+1 日数据**（避免假判可执行性） |

主流量化框架默认都是 T+1 开盘成交，因为这最接近实盘节奏（散户盘尾决策，次日开盘成交）。

### 2. 优化目标：Sharpe 比率

| 维度 | 旧版 | 新版 |
|------|------|------|
| 优化目标 | 综合分数 = 0.5×收益 + 0.3×胜率 - 0.2×回撤 | **Sharpe 比率**（最大化） |
| 缺点 | 主观权重，非主流 | 主流默认，平衡收益与风险 |
| 可选 | — | 也支持 Calmar / Sortino / total_return |

### 3. 样本切分：Walk-Forward 防过拟合

| 维度 | 旧版 | 新版 |
|------|------|------|
| 切分方式 | 全样本搜索 | **Walk-Forward 滚动窗口** |
| 默认窗口 | — | train=60d / test=20d / step=20d |
| 过拟合风险 | 高（必然过拟合） | **低**（OOS 验证） |
| 输出 | 单组最优参数 | IS + OOS 双指标 + 综合 voted 参数 |

### 4. 阈值参数：6 → 17 个

| 类别 | 旧版参数 | 新版参数 |
|------|---------|---------|
| 进场严格度 | panic_min, arbitrage_min | panic_min, arbitrage_min, **momentum_min** |
| 出场阈值 | take_profit, ma5_pressure, confess_wrong_days, max_hold_days | 同左 |
| 风控 | — | **stop_loss_pct, min_hold_days, cooldown_days_after_sell** |
| 加仓 | — | **add_position_trigger_pct, add_position_min_days** |
| 量价触发 | — | **volume_ratio_threshold, drop_5d_threshold, gain_today_threshold** |
| 板块强度 | sector_strong, sector_weak | 同左 |
| 仓位 | budget_per_stock, batch_count | + **position_mode (equal_weight/batch), max_concurrent_positions** |

### 5. 仓位管理：等权分配

| 维度 | 旧版 | 新版 |
|------|------|------|
| 默认模式 | batch（分批） | **equal_weight**（等权） |
| 优点 | 摊薄成本 | 主流、透明、易对比 |
| 单股预算 | 25 万 / 3 批 | 25 万（一次性） |

### 6. 基准对比：沪深300 Alpha/Beta/IR

| 维度 | 旧版 | 新版 |
|------|------|------|
| 基准 | 无 | **沪深300 买入持有** |
| Alpha | — | **CAPM 年化 Alpha** |
| Beta | — | **CAPM Beta** |
| 信息比率 | — | **IR = Alpha / 跟踪误差** |
| 跟踪误差 | — | **年化跟踪误差%** |

### 7. 完整指标输出

新指标模块 `metrics.py` 提供主流全指标：

- **收益类**: total_return_pct, annual_return_pct
- **风险调整**: Sharpe, Sortino, Calmar
- **风险**: max_drawdown, annual_volatility, downside_volatility
- **基准对比**: Alpha, Beta, Information Ratio, Tracking Error
- **交易**: win_rate, profit_factor, avg_win/loss, trade_count

## 新增/重写文件清单

### 新增

| 文件 | 作用 |
|------|------|
| `src/loop/metrics.py` | 标准化指标计算（与 Pyfolio 对齐） |
| `src/loop/walk_forward.py` | Walk-Forward 滚动窗口优化框架 |
| `scripts/test_metrics.py` | 指标单元测试（10 个测试用例） |
| `scripts/test_integration.py` | 集成测试（6 个测试用例，合成数据） |
| `LOOP_REWRITE_README.md` | 本说明文档 |

### 重写

| 文件 | 改动要点 |
|------|---------|
| `src/loop/__init__.py` | 模块导出（统一 API 入口） |
| `src/loop/backtest_engine.py` | T+1 次日开盘成交 + 基准对比 + 完整指标 + 向后兼容 |
| `src/loop/stockagent_tuned_v3_signals.py` | 17 阈值参数 + 等权仓位 + T+1 兼容 |
| `scripts/run_grid_search.py` | Walk-Forward + Sharpe 目标 + JSON 报告 |

### 保留不动

- `src/loop/data_loader.py`（无需改动，已支持 K 线加载）
- `src/loop/market_mode_adaptive.py`（无需改动，自适应模式逻辑保持）

## 使用示例

### 1. 单次回测

```python
from src.loop.data_loader import DataLoader
from src.loop.backtest_engine import BacktestEngine, print_result
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, TUNE_PARAMS_V3

loader = DataLoader()
kline = loader.load_kline(["688256"], "2025-11-01", "2026-04-30")
bench = loader._akshare.get_index_data("000300").data

gen = StockAgentTunedV3Signals(
    params={**TUNE_PARAMS_V3, "backtest_mode": True},
)
signals = gen.generate_signals(kline)

engine = BacktestEngine(initial_cash=1_000_000)
result = engine.run(signals, kline, benchmark_kline=bench)
print_result(result, title="回测结果")
```

### 2. Walk-Forward 网格搜索（命令行）

```bash
# 完整搜索（17 维，约 5000+ 组合）
python3 scripts/run_grid_search.py

# 快速测试（8 维，约 256 组合）
python3 scripts/run_grid_search.py --quick

# 自定义窗口
python3 scripts/run_grid_search.py --train 80 --test 30 --step 30

# 自定义股票池
python3 scripts/run_grid_search.py --codes 688256,301005,301232
```

### 3. 编程式 Walk-Forward

```python
from src.loop.walk_forward import WalkForwardOptimizer
from src.loop.backtest_engine import BacktestEngine
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, TUNE_PARAMS_V3

wf = WalkForwardOptimizer(
    kline_data=kline_data,
    benchmark_kline=bench,
    train_window=60,
    test_window=20,
    step=20,
    initial_cash=1_000_000,
    engine_factory=lambda: BacktestEngine(initial_cash=1_000_000),
    signal_factory=lambda p: StockAgentTunedV3Signals(
        params={**TUNE_PARAMS_V3, **p, "backtest_mode": True}
    ),
    objective="sharpe",
)

result = wf.run_grid_search(grid={
    "panic_min_conditions": [2, 3],
    "take_profit_threshold": [0.05, 0.08, 0.10],
    "max_hold_days": [10, 15, 20],
})

# result.oos_sharpe_mean: 样本外平均 Sharpe（真实表现）
# result.is_sharpe_mean: 样本内平均 Sharpe（参考）
# result.best_params_overall: 综合最优参数
# result.folds: 每个 fold 的明细
```

## 测试

```bash
# 指标单元测试
python3 scripts/test_metrics.py

# 集成测试（合成数据，无需 API）
python3 scripts/test_integration.py
```

所有测试通过 ✅

## 主流对齐检查清单

- [x] **T+1 次日开盘成交**（消除前视偏差，与 Zipline/Backtrader 一致）
- [x] **Sharpe 比率优化目标**（主流默认）
- [x] **Walk-Forward 样本外验证**（防止过拟合）
- [x] **完整 A 股约束**（T+1 卖出限制、涨跌停、100 股最小单位、滑点+佣金+印花税）
- [x] **沪深300 基准对比**（Alpha/Beta/IR/跟踪误差）
- [x] **完整指标输出**（Sharpe/Sortino/Calmar/Alpha/Beta/IR/最大回撤/波动率）
- [x] **15+ 阈值参数网格搜索**（覆盖进场/出场/风控/加仓/量价/板块全维度）
- [x] **等权仓位管理**（最主流最透明）
- [x] **向后兼容**（BacktestResult 保留旧字段访问）
- [x] **JSON 报告输出**（每个 fold 完整指标 + 聚合 OOS 表现）

## 关键算法说明

### Sharpe 比率计算

```
Sharpe = (mean_daily_excess_return) / std_daily_excess_return × √252

其中：
  daily_excess_return = daily_return - rf_daily
  rf_daily = (1 + risk_free_rate)^(1/252) - 1
```

### Alpha/Beta 计算（CAPM）

```
Beta = Cov(R_strategy, R_benchmark) / Var(R_benchmark)
Alpha_daily = mean(R_strategy - rf) - Beta × mean(R_benchmark - rf)
Alpha_annual = Alpha_daily × 252 × 100  (转年化%)
```

### Walk-Forward 流程

```
Fold 1: train[0:60]  → 找最优参数 P1 → test[60:80]  → 用 P1 跑出 OOS1
Fold 2: train[20:80] → 找最优参数 P2 → test[80:100] → 用 P2 跑出 OOS2
Fold 3: train[40:100]→ 找最优参数 P3 → test[100:120]→ 用 P3 跑出 OOS3
...
最终 OOS = mean(OOS1, OOS2, ...) ± std
```

### 综合最优参数投票

每个 fold 的 best_params 投一票，权重 = max(0.01, OOS_Sharpe)
每个参数维度独立投票，得票最多的值胜出。

## 后续可扩展方向

1. **多基准对比**：同时跑沪深300/中证500/中证2000
2. **K-Fold CV**：5 折时序交叉验证（替代 Walk-Forward）
3. **多目标优化**：同时最大化 Sharpe + 最小化回撤（Pareto 前沿）
4. **贝叶斯优化**：替代网格搜索（Optuna/Hyperopt）
5. **HTML 报告**：含净值曲线/回撤曲线/参数热力图
6. **波动率目标仓位**：基于 ATR 的风险平价
7. **Kelly 公式仓位**：根据胜率×盈亏比计算最优仓位
