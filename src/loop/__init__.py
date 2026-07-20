"""
回测引擎模块 v2 — 主流计算逻辑对齐版

【模块组成】

- metrics.py:                     标准化指标计算（Sharpe/Sortino/Calmar/Alpha/Beta/IR）
- backtest_engine.py:             A股回测引擎（T+1次日开盘成交 + 涨跌停/T+1约束 + 完整指标）
- data_loader.py:                 数据加载器（用现有 akshare_adapter 拉历史K线）
- stockagent_tuned_v3_signals.py: 策略信号生成（15+阈值参数 + 等权仓位）
- market_mode_adaptive.py:        多模式自适应（attack/defend/retreat 按日切换）
- walk_forward.py:                Walk-Forward 滚动窗口优化框架（防过拟合）

【主流对齐要点】

1. **T+1 次日开盘成交**（消除前视偏差，与 Zipline/Backtrader 一致）
2. **Sharpe 比率为优化目标**（替代原"综合分数"）
3. **Walk-Forward 样本外验证**（防止过拟合）
4. **完整指标输出**（Sharpe/Sortino/Calmar/Alpha/Beta/IR/跟踪误差）
5. **沪深300基准对比**（计算超额收益）
6. **15+ 阈值参数网格搜索**（覆盖进场/出场/风控/加仓/量价/板块全维度）
7. **等权仓位管理**（最主流最透明）

【使用示例】

    from src.loop.data_loader import DataLoader
    from src.loop.backtest_engine import BacktestEngine, print_result
    from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, TUNE_PARAMS_V3
    from src.loop.walk_forward import WalkForwardOptimizer

    # 单次回测
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

    # Walk-Forward 网格搜索
    wf = WalkForwardOptimizer(
        kline_data=kline,
        benchmark_kline=bench,
        train_window=60, test_window=20, step=20,
        engine_factory=lambda: BacktestEngine(initial_cash=1_000_000),
        signal_factory=lambda p: StockAgentTunedV3Signals(
            params={**TUNE_PARAMS_V3, **p, "backtest_mode": True}
        ),
        objective="sharpe",
    )
    wf_result = wf.run_grid_search(grid={
        "panic_min_conditions": [2, 3],
        "take_profit_threshold": [0.05, 0.08],
    })
"""

# 模块导出
from .metrics import (
    Metrics,
    calc_all_metrics,
    calc_sharpe,
    calc_sortino,
    calc_max_drawdown,
    calc_alpha_beta,
    calc_trade_stats,
    build_benchmark_curve,
    print_metrics,
)
from .backtest_engine import (
    BacktestEngine,
    BacktestResult,
    Signal,
    Trade,
    Position,
    print_result,
    get_limit_ratio,
    apply_slippage,
    is_limit_up,
    is_limit_down,
    SLIPPAGE_BPS,
    COMMISSION_RATE,
    STAMP_DUTY_RATE,
    LOT_SIZE,
)
from .stockagent_tuned_v3_signals import (
    StockAgentTunedV3Signals,
    DEFAULT_BACKTEST_PARAMS,
    STOCK_SECTOR_MAP,
)
from .walk_forward import (
    WalkForwardOptimizer,
    WalkForwardResult,
    FoldResult,
    build_grid_combinations,
)
from .data_loader import DataLoader
from .market_mode_adaptive import (
    MarketModeAdaptive,
    get_market_mode_adaptive,
)

__all__ = [
    # metrics
    "Metrics", "calc_all_metrics", "calc_sharpe", "calc_sortino",
    "calc_max_drawdown", "calc_alpha_beta", "calc_trade_stats",
    "build_benchmark_curve", "print_metrics",
    # backtest_engine
    "BacktestEngine", "BacktestResult", "Signal", "Trade", "Position",
    "print_result", "get_limit_ratio", "apply_slippage",
    "is_limit_up", "is_limit_down",
    "SLIPPAGE_BPS", "COMMISSION_RATE", "STAMP_DUTY_RATE", "LOT_SIZE",
    # signals
    "StockAgentTunedV3Signals", "DEFAULT_BACKTEST_PARAMS", "STOCK_SECTOR_MAP",
    # walk_forward
    "WalkForwardOptimizer", "WalkForwardResult", "FoldResult",
    "build_grid_combinations",
    # data_loader
    "DataLoader",
    # market_mode_adaptive
    "MarketModeAdaptive", "get_market_mode_adaptive",
]
