from datetime import date

from pointtable.data.guard import (
    DataFreshnessGuard,
    check_volume_caliber,
    find_recent_trading_day,
)


def test_freshness_requires_expected_trading_day():
    guard = DataFreshnessGuard()
    assert guard.check_source([{'trade_date': '2026-01-02'}], now=date(2026, 1, 2))
    assert not guard.check_source([{'trade_date': '2026-01-01'}], now=date(2026, 1, 2))
    assert guard.check_fresh_enough('2025-12-30', now=date(2026, 1, 2))


def test_recent_trading_day_skips_weekend():
    assert find_recent_trading_day(date(2026, 1, 5)) == date(2026, 1, 5)
    assert find_recent_trading_day(date(2026, 1, 4)) == date(2026, 1, 2)


def test_dirty_data_gates():
    assert not check_volume_caliber(0.8, 20.0).ok
    assert not check_volume_caliber(1.0, 1.0, turnover_rate=4.0, volume_shares=2_000_000_000).ok
    assert not check_volume_caliber(1.0, 1.0, kline_volume_ratio=2.0).ok
    assert check_volume_caliber(1.0, 1.0, turnover_rate=15.0, volume_shares=2_000_000_000).ok
