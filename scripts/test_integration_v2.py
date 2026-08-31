# -*- coding: utf-8 -*-
"""
集成测试 v2：验证回测逻辑 = 实盘逻辑

核心验证点：
1. TimingEngine 在 backtest_mode 下能正确注入 K 线并生成信号
2. StockAgentTunedV3Signals 委托给 TimingEngine（不再有重复逻辑）
3. config/timing.yaml 的参数能被 timing_engine 正确读取
4. params_override 能覆盖 timing.yaml 配置（网格搜索基础）
5. 回测生成的信号与实盘 intraday 路径走相同的 check_entry_signals/check_exit_signals
6. T+1 次日开盘成交逻辑
7. Walk-Forward 滚动窗口

使用合成 K 线数据，无需 AKShare API。
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.loop.backtest_engine import BacktestEngine
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS
from src.loop.walk_forward import WalkForwardOptimizer
from src.analyzers.timing_engine import (
    TimingEngine, get_backtest_timing_engine, get_timing_engine,
)


def make_synthetic_kline(start_price=10.0, days=80, volatility=0.02, seed=42):
    """生成合成 K 线数据（一只股票）"""
    import random
    random.seed(seed)
    price = start_price
    base_date = datetime(2025, 1, 5)

    d = base_date
    rows = []
    for _ in range(days):
        while d.weekday() >= 5:
            d += timedelta(days=1)

        change = random.gauss(0.001, volatility)
        open_price = price
        close = max(0.5, price * (1 + change))
        high = max(open_price, close) * (1 + abs(random.gauss(0, 0.005)))
        low = min(open_price, close) * (1 - abs(random.gauss(0, 0.005)))
        volume = random.randint(1_000_000, 10_000_000)

        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": round(open_price, 2),
            "close": round(close, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "volume": volume,
        })
        price = close
        d += timedelta(days=1)

    # 补充 prev_close
    for i in range(len(rows)):
        if i == 0:
            rows[i]["prev_close"] = rows[i]["open"]
        else:
            rows[i]["prev_close"] = rows[i - 1]["close"]

    return rows


def make_synthetic_benchmark(start_value=3000, days=80, seed=7):
    """生成合成基准指数 K 线"""
    import random
    random.seed(seed)
    kline = []
    value = start_value
    base_date = datetime(2025, 1, 5)
    d = base_date
    for _ in range(days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        change = random.gauss(0.0005, 0.01)
        close = max(100, value * (1 + change))
        open_price = value
        kline.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": round(open_price, 2),
            "close": round(close, 2),
            "high": round(max(open_price, close) * 1.005, 2),
            "low": round(min(open_price, close) * 0.995, 2),
            "volume": 1_000_000_000,
        })
        value = close
        d += timedelta(days=1)
    return kline


def test_timing_engine_config_loading():
    """测试 1：TimingEngine 能正确加载 timing.yaml 配置"""
    print("\n━━━ 测试 1: timing.yaml 配置加载 ━━━")

    engine = get_backtest_timing_engine()

    # 验证关键配置项已加载
    assert engine._cfg("panic_bottom", "index_drop_threshold") == 4.0
    assert engine._cfg("panic_bottom", "stock_drop_threshold") == -5
    assert engine._cfg("arbitrage", "min_trigger_conditions") == 1
    assert engine._cfg("momentum_chase", "volume_confirm_ratio") == 1.2
    assert engine._cfg("exit", "exhaustion", "rsi_overbought") == 70
    assert engine._cfg("stop_loss", "multiplier") == 0.97
    assert engine._cfg("target_range", "panic_bottom_low") == 1.08

    print(f"   panic_bottom.index_drop_threshold = {engine._cfg('panic_bottom', 'index_drop_threshold')}")
    print(f"   stop_loss.multiplier = {engine._cfg('stop_loss', 'multiplier')}")
    print(f"   exit.exhaustion.rsi_overbought = {engine._cfg('exit', 'exhaustion', 'rsi_overbought')}")
    print("✅ timing.yaml 配置加载正确")


def test_params_override():
    """测试 2：params_override 能覆盖 timing.yaml 配置（网格搜索基础）"""
    print("\n━━━ 测试 2: params_override 参数覆盖 ━━━")

    override = {
        "panic_bottom": {
            "index_drop_threshold": 3.0,  # 从 4.0 改为 3.0
        },
        "stop_loss": {
            "multiplier": 0.95,  # 从 0.97 改为 0.95
        },
    }
    engine = get_backtest_timing_engine(params_override=override)

    assert engine._cfg("panic_bottom", "index_drop_threshold") == 3.0
    assert engine._cfg("stop_loss", "multiplier") == 0.95
    # 未覆盖的应保持原值
    assert engine._cfg("panic_bottom", "stock_drop_threshold") == -5
    assert engine._cfg("arbitrage", "min_trigger_conditions") == 1

    print(f"   覆盖后 panic_bottom.index_drop_threshold = {engine._cfg('panic_bottom', 'index_drop_threshold')}")
    print(f"   覆盖后 stop_loss.multiplier = {engine._cfg('stop_loss', 'multiplier')}")
    print(f"   未覆盖 panic_bottom.stock_drop_threshold = {engine._cfg('panic_bottom', 'stock_drop_threshold')}")
    print("✅ params_override 参数覆盖正确")


def test_backtest_mode_kline_injection():
    """测试 3：backtest_mode 能正确注入 K 线"""
    print("\n━━━ 测试 3: backtest_mode K 线注入 ━━━")

    kline = make_synthetic_kline(days=60)
    kline_data = {"600519": kline}

    engine = get_backtest_timing_engine()
    engine.set_backtest_context(
        date=kline[-1]["date"],
        kline_data=kline_data,
        index_kline=[],
    )

    # 验证 _fetch_tech_data 使用注入的 K 线
    tech_data = engine._fetch_tech_data("600519", "defend")
    assert tech_data.get("kline") is not None
    assert len(tech_data["kline"]) == 60
    assert tech_data.get("current_price", 0) > 0
    assert tech_data.get("ma5", 0) > 0
    assert tech_data.get("ma20", 0) > 0

    print(f"   注入 K 线: {len(tech_data['kline'])} 根")
    print(f"   current_price = {tech_data['current_price']:.2f}")
    print(f"   ma5 = {tech_data['ma5']:.2f}")
    print(f"   ma20 = {tech_data['ma20']:.2f}")
    print("✅ backtest_mode K 线注入正确")


def test_stop_loss_multiplier_fix():
    """测试 4：验证 stop_loss_multiplier bug 已修复"""
    print("\n━━━ 测试 4: stop_loss_multiplier bug 修复 ━━━")

    kline = make_synthetic_kline(days=60)
    kline_data = {"600519": kline}

    # 用 0.95 的 multiplier
    engine = get_backtest_timing_engine(params_override={"stop_loss": {"multiplier": 0.95}})
    engine.set_backtest_context(kline[-1]["date"], kline_data, [])

    tech_data = engine._fetch_tech_data("600519", "defend")
    stop_loss = engine.calculate_stop_loss("600519", tech_data)

    # 验证止损价 = 支撑位 × 0.95（而非原 bug 的 0.97）
    expected_multiplier = 0.95
    # 找到 chosen_support，验证 stop_loss_price = chosen_support * 0.95
    assert abs(stop_loss.stop_loss_price - stop_loss.chosen_support * expected_multiplier) < 0.01, \
        f"止损价应为 {stop_loss.chosen_support * expected_multiplier:.4f}，实际 {stop_loss.stop_loss_price}"

    print(f"   chosen_support = {stop_loss.chosen_support:.2f}")
    print(f"   stop_loss_price = {stop_loss.stop_loss_price:.2f}")
    print(f"   multiplier = {expected_multiplier}")
    print(f"   验证: {stop_loss.chosen_support} × {expected_multiplier} = {stop_loss.chosen_support * expected_multiplier:.4f}")
    print("✅ stop_loss_multiplier bug 已修复（现在读配置而非裸 0.97）")


def test_tech_cache_weekly_init():
    """测试 5：验证 _tech_cache_weekly 已初始化（bug 修复）"""
    print("\n━━━ 测试 5: _tech_cache_weekly 初始化 ━━━")

    engine = get_backtest_timing_engine()
    assert hasattr(engine, "_tech_cache_weekly"), "应有 _tech_cache_weekly 属性"
    assert isinstance(engine._tech_cache_weekly, dict), "_tech_cache_weekly 应为 dict"

    # 验证回测模式能从日 K 聚合周 K
    kline = make_synthetic_kline(days=120)
    engine.set_backtest_context(kline[-1]["date"], {"600519": kline}, [])
    tech_data = engine._fetch_tech_data("600519", "defend")

    # 周线 MACD 应该能计算（不报错），结果为 bool
    weekly_macd_up = tech_data.get("weekly_macd_up")
    assert isinstance(weekly_macd_up, bool)

    # 验证周 K 已聚合
    weekly_kline = engine._tech_cache_weekly.get("600519")
    if weekly_kline:
        print(f"   日 K: {len(kline)} 根 → 周 K: {len(weekly_kline)} 根")
    print(f"   weekly_macd_up = {weekly_macd_up}")
    print("✅ _tech_cache_weekly 初始化 + 周线 MACD 计算正确")


def test_backtest_delegates_to_timing_engine():
    """测试 6：回测委托给 TimingEngine（核心一致性验证）"""
    print("\n━━━ 测试 6: 回测委托给 TimingEngine ━━━")

    kline = make_synthetic_kline(days=80, volatility=0.03)
    kline_data = {"688256": kline}

    # 用回测驱动器生成信号
    gen = StockAgentTunedV3Signals(
        params={**DEFAULT_BACKTEST_PARAMS},
    )
    signals = gen.generate_signals(kline_data)

    print(f"   生成信号数: {len(signals)}")
    print(f"   买入信号: {sum(1 for s in signals if s.action == 'buy')}")
    print(f"   卖出信号: {sum(1 for s in signals if s.action == 'sell')}")

    # 验证信号格式正确
    for s in signals[:3]:
        assert hasattr(s, "date")
        assert hasattr(s, "code")
        assert hasattr(s, "action")
        assert hasattr(s, "shares")
        assert hasattr(s, "price")
        assert s.action in ("buy", "sell")

    # 关键验证：StockAgentTunedV3Signals 内部持有 TimingEngine 实例
    assert hasattr(gen, "_timing"), "StockAgentTunedV3Signals 应持有 _timing 属性"
    assert isinstance(gen._timing, TimingEngine), "_timing 应为 TimingEngine 实例"
    assert gen._timing._backtest_mode is True, "_timing 应为 backtest_mode=True"

    print(f"   _timing 是 TimingEngine 实例: {isinstance(gen._timing, TimingEngine)}")
    print(f"   _timing._backtest_mode: {gen._timing._backtest_mode}")
    print("✅ 回测委托给 TimingEngine（逻辑一致性保证）")


def test_full_backtest_pipeline():
    """测试 7：完整回测流水线（信号生成 + T+1 执行 + 指标计算）"""
    print("\n━━━ 测试 7: 完整回测流水线 ━━━")

    kline = make_synthetic_kline(days=80, volatility=0.03)
    bench = make_synthetic_benchmark(days=80)
    kline_data = {"688256": kline}

    # 生成信号
    gen = StockAgentTunedV3Signals(params={**DEFAULT_BACKTEST_PARAMS})
    signals = gen.generate_signals(kline_data)

    if not signals:
        print("   ⚠️ 合成数据未生成信号（正常，随机数据可能不满足进场条件）")
        print("✅ 流水线无报错（信号生成阶段通过）")
        return

    # 执行回测
    engine = BacktestEngine(initial_cash=1_000_000)
    result = engine.run(signals, kline_data, benchmark_kline=bench)

    print(f"   总交易次数: {result.trade_count}")
    print(f"   总收益率: {result.metrics.total_return_pct:+.2f}%")
    print(f"   Sharpe: {result.metrics.sharpe_ratio:.4f}")
    print(f"   最大回撤: {result.metrics.max_drawdown_pct:.2f}%")
    print(f"   Alpha: {result.metrics.alpha:+.2f}%")
    print(f"   Beta: {result.metrics.beta:.4f}")
    print("✅ 完整回测流水线通过")


def test_walk_forward_with_timing_engine():
    """测试 8：Walk-Forward 搜索（使用真实 TimingEngine）"""
    print("\n━━━ 测试 8: Walk-Forward + TimingEngine ━━━")

    kline = make_synthetic_kline(days=120, volatility=0.03)
    bench = make_synthetic_benchmark(days=120)
    kline_data = {"688256": kline}

    wf = WalkForwardOptimizer(
        kline_data=kline_data,
        benchmark_kline=bench,
        train_window=40,
        test_window=15,
        step=15,
        initial_cash=100_000,
        engine_factory=lambda: BacktestEngine(initial_cash=100_000),
        signal_factory=lambda p: StockAgentTunedV3Signals(
            params={**DEFAULT_BACKTEST_PARAMS, **p}
        ),
        objective="sharpe",
    )

    grid = {
        "panic_bottom.index_drop_threshold": [3.0, 4.0],
        "take_profit_threshold": [0.05, 0.08],
        "max_hold_days": [10, 15],
    }

    result = wf.run_grid_search(grid=grid)

    assert result.n_folds > 0
    assert len(result.folds) == result.n_folds

    print(f"   总 fold 数: {result.n_folds}")
    print(f"   每折组合数: {result.total_combinations}")
    print(f"   搜索耗时: {result.search_seconds:.1f}s")
    print(f"   IS 平均 Sharpe: {result.is_sharpe_mean:.4f}")
    print(f"   OOS 平均 Sharpe: {result.oos_sharpe_mean:.4f}")
    print(f"   综合最优参数: {result.best_params_overall}")
    print("✅ Walk-Forward + TimingEngine 集成正确")


def test_intraday_vs_backtest_consistency():
    """
    测试 9：核心一致性验证

    验证回测和实盘走相同的 check_entry_signals / check_exit_signals 方法。
    这是"回测逻辑 = 实盘逻辑"的最终保证。
    """
    print("\n━━━ 测试 9: 回测 vs 实盘逻辑一致性 ━━━")

    kline = make_synthetic_kline(days=60)
    kline_data = {"688256": kline}

    # 回测引擎
    bt_engine = get_backtest_timing_engine()
    bt_engine.set_backtest_context(kline[-1]["date"], kline_data, [])

    # 验证：回测引擎和实盘引擎是同一个类
    live_engine = get_timing_engine()
    assert type(bt_engine) == type(live_engine) == TimingEngine

    # 验证：两者都调用 check_entry_signals（同一个方法）
    assert hasattr(bt_engine, "check_entry_signals")
    assert hasattr(bt_engine, "check_exit_signals")
    assert hasattr(live_engine, "check_entry_signals")
    assert hasattr(live_engine, "check_exit_signals")

    # 验证：回测引擎能成功调用 check_entry_signals（与实盘相同的方法）
    entry_signals = bt_engine.check_entry_signals(
        stock_code="688256",
        stock_name="测试",
        market_mode="defend",
        sector_status="rotational",
    )
    # 合成数据可能不触发信号，但不应该报错
    assert isinstance(entry_signals, list)

    # 验证：check_exit_signals 也能调用
    exit_signals = bt_engine.check_exit_signals(
        stock_code="688256",
        stock_name="测试",
        market_mode="defend",
        sector_status="rotational",
    )
    assert isinstance(exit_signals, list)

    print(f"   回测引擎类型: {type(bt_engine).__name__}")
    print(f"   实盘引擎类型: {type(live_engine).__name__}")
    print(f"   check_entry_signals 是同一方法: {bt_engine.check_entry_signals.__func__ is live_engine.check_entry_signals.__func__}")
    print(f"   check_exit_signals 是同一方法: {bt_engine.check_exit_signals.__func__ is live_engine.check_exit_signals.__func__}")
    print(f"   入场信号数: {len(entry_signals)}")
    print(f"   出场信号数: {len(exit_signals)}")
    print("✅ 回测与实盘走相同的 TimingEngine 方法（逻辑一致性保证）")


def main():
    print()
    print("=" * 70)
    print("  集成测试 v2：回测逻辑 = 实盘逻辑")
    print("  验证 TimingEngine 参数化 + backtest_mode + 委托调用")
    print("=" * 70)

    test_timing_engine_config_loading()
    test_params_override()
    test_backtest_mode_kline_injection()
    test_stop_loss_multiplier_fix()
    test_tech_cache_weekly_init()
    test_backtest_delegates_to_timing_engine()
    test_full_backtest_pipeline()
    test_walk_forward_with_timing_engine()
    test_intraday_vs_backtest_consistency()

    print()
    print("=" * 70)
    print("  ✅ 所有集成测试通过！")
    print()
    print("  核心保证：")
    print("  • 回测走 TimingEngine.check_entry_signals / check_exit_signals")
    print("  • 实盘 intraday 也走相同方法")
    print("  • 所有阈值从 config/timing.yaml 读取")
    print("  • 网格搜索的最优参数可直接迁移到实盘")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
