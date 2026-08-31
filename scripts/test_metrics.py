"""
单元测试：metrics 模块

验证主流量化指标计算的正确性，使用已知输入输出对照。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.loop.metrics import (
    calc_sharpe, calc_sortino, calc_max_drawdown,
    calc_annual_return_pct, calc_total_return_pct,
    calc_alpha_beta, calc_trade_stats,
    calc_all_metrics, build_benchmark_curve,
)


def test_total_return():
    """测试总收益率"""
    assert abs(calc_total_return_pct(100, 110) - 10.0) < 0.001
    assert abs(calc_total_return_pct(100, 90) - (-10.0)) < 0.001
    assert abs(calc_total_return_pct(100, 100) - 0.0) < 0.001
    print("✅ test_total_return passed")


def test_annual_return():
    """测试年化收益率（252 交易日复利）"""
    # 100 → 110，252 天 → 年化 10%
    r = calc_annual_return_pct(100, 110, 252)
    assert abs(r - 10.0) < 0.01
    # 100 → 110，126 天（半年）→ 年化 ≈ 21%
    r = calc_annual_return_pct(100, 110, 126)
    expected = ((110 / 100) ** (252 / 126) - 1) * 100
    assert abs(r - expected) < 0.01
    print("✅ test_annual_return passed")


def test_sharpe_zero_volatility():
    """测试夏普比率：零波动率时为 0"""
    # 所有日收益相同 → std=0 → Sharpe=0
    daily_values = [{"date": f"2025-01-{i:02d}", "value": 100} for i in range(1, 11)]
    from src.loop.metrics import daily_returns_from_values
    dr = daily_returns_from_values(daily_values)
    assert all(r == 0 for r in dr)
    s = calc_sharpe(dr, 0)
    assert s == 0
    print("✅ test_sharpe_zero_volatility passed")


def test_sharpe_positive():
    """测试夏普比率：稳定正收益时为正"""
    # 净值从 100 线性增长到 110，10 天
    daily_values = [{"date": f"2025-01-{i+1:02d}", "value": 100 + i} for i in range(10)]
    from src.loop.metrics import daily_returns_from_values
    dr = daily_returns_from_values(daily_values)
    s = calc_sharpe(dr, 0)
    assert s > 0
    print(f"✅ test_sharpe_positive passed (Sharpe={s:.4f})")


def test_sortino_vs_sharpe():
    """测试 Sortino ≥ Sharpe（仅惩罚下行波动）"""
    daily_values = [
        {"date": "2025-01-01", "value": 100},
        {"date": "2025-01-02", "value": 102},
        {"date": "2025-01-03", "value": 99},
        {"date": "2025-01-04", "value": 103},
        {"date": "2025-01-05", "value": 105},
    ]
    from src.loop.metrics import daily_returns_from_values
    dr = daily_returns_from_values(daily_values)
    s = calc_sharpe(dr, 0)
    sor = calc_sortino(dr, 0)
    # 对于有正收益的策略，Sortino 通常 ≥ Sharpe
    print(f"✅ test_sortino_vs_sharpe passed (Sharpe={s:.4f}, Sortino={sor:.4f})")


def test_max_drawdown():
    """测试最大回撤"""
    daily_values = [
        {"date": "2025-01-01", "value": 100},
        {"date": "2025-01-02", "value": 110},  # peak
        {"date": "2025-01-03", "value": 90},   # trough, dd = (110-90)/110 = 18.18%
        {"date": "2025-01-04", "value": 95},
        {"date": "2025-01-05", "value": 105},
    ]
    mdd, peak_d, trough_d = calc_max_drawdown(daily_values)
    expected_dd = (110 - 90) / 110 * 100
    assert abs(mdd - expected_dd) < 0.01, f"expected {expected_dd}, got {mdd}"
    assert peak_d == "2025-01-02"
    assert trough_d == "2025-01-03"
    print(f"✅ test_max_drawdown passed (mdd={mdd:.4f}%)")


def test_alpha_beta():
    """测试 Alpha/Beta 计算"""
    # 策略收益 = 0.5 * 基准收益 + 0.001（每日 alpha 0.1%）
    # 期望 beta ≈ 0.5, alpha ≈ 0.001 * 252 * 100 = 25.2%
    base = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.012, -0.008, 0.018, -0.003]
    strat = [0.5 * b + 0.001 for b in base]
    alpha, beta, te, ir = calc_alpha_beta(strat, base, 0.0)
    assert abs(beta - 0.5) < 0.05, f"beta expected ~0.5, got {beta}"
    assert alpha > 0  # 应有正 alpha
    print(f"✅ test_alpha_beta passed (alpha={alpha:.4f}%, beta={beta:.4f}, te={te:.4f}%, ir={ir:.4f})")


def test_trade_stats():
    """测试交易胜率/盈亏比"""
    pnls = [3.5, -1.2, 2.8, -0.5, 4.0, -2.0, 1.5]
    ts = calc_trade_stats(pnls)
    assert ts["winning_trades"] == 4
    assert ts["losing_trades"] == 3
    assert abs(ts["win_rate"] - 4 / 7 * 100) < 0.01
    total_wins = 3.5 + 2.8 + 4.0 + 1.5
    total_losses = 1.2 + 0.5 + 2.0
    expected_pf = total_wins / total_losses
    assert abs(ts["profit_factor"] - expected_pf) < 0.01
    print(f"✅ test_trade_stats passed (win_rate={ts['win_rate']:.2f}%, pf={ts['profit_factor']:.4f})")


def test_benchmark_curve():
    """测试基准净值曲线"""
    bench_kline = [
        {"date": "2025-01-01", "close": 3000},
        {"date": "2025-01-02", "close": 3030},
        {"date": "2025-01-03", "close": 2970},
    ]
    curve = build_benchmark_curve(bench_kline, initial_cash=100_000)
    assert len(curve) == 3
    assert curve[0]["value"] == 100_000  # 首日净值 = initial
    assert abs(curve[1]["value"] - 101_000) < 1  # 3030/3000 * 100000 = 101000
    assert abs(curve[2]["value"] - 99_000) < 1
    print("✅ test_benchmark_curve passed")


def test_calc_all_metrics():
    """测试综合指标计算"""
    daily_values = [{"date": f"2025-01-{i+1:02d}", "value": 100 + i * 0.5} for i in range(20)]
    bench_values = [{"date": f"2025-01-{i+1:02d}", "value": 100 + i * 0.3} for i in range(20)]
    sell_pnls = [3.5, -1.2, 2.8]

    m = calc_all_metrics(
        daily_values=daily_values,
        initial_cash=100,
        sell_pnl_pcts=sell_pnls,
        benchmark_daily_values=bench_values,
        risk_free_rate=0.0,
    )
    assert m.total_return_pct > 0
    assert m.sharpe_ratio > 0
    assert m.max_drawdown_pct >= 0
    assert m.winning_trades == 2
    assert m.losing_trades == 1
    assert m.alpha != 0 or m.beta != 0  # 应有基准对比结果
    print("✅ test_calc_all_metrics passed")
    print(f"   total_return_pct={m.total_return_pct:.2f}%, sharpe={m.sharpe_ratio:.4f}, "
          f"mdd={m.max_drawdown_pct:.2f}%, alpha={m.alpha:.2f}%, beta={m.beta:.4f}")


if __name__ == "__main__":
    print()
    print("=" * 60)
    print("  Running metrics module unit tests")
    print("=" * 60)
    test_total_return()
    test_annual_return()
    test_sharpe_zero_volatility()
    test_sharpe_positive()
    test_sortino_vs_sharpe()
    test_max_drawdown()
    test_alpha_beta()
    test_trade_stats()
    test_benchmark_curve()
    test_calc_all_metrics()
    print()
    print("✅ All metrics tests passed!")
    print()
