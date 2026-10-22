import pytest

from pointtable.stats import behavior, signal_quality


def test_signal_quality_uses_confirmed_only_for_expectancy():
    rows = [
        {'strategy_id': 'a', 'status': 'TRIGGERED', 'confirmed': True, 'hypothetical_pnl_pct': 2.0},
        {'strategy_id': 'a', 'status': 'TRIGGERED', 'confirmed': False, 'hypothetical_pnl_pct': 100.0},
        {'strategy_id': 'a', 'status': 'ACTIVE', 'confirmed': False},
    ]
    report = signal_quality(rows)['a']
    assert report['confirmed'] == 1
    assert report['confirmed_expectancy'] == 2.0
    assert not report['ev_negative']


def test_behavior_reports_insufficient_coverage_by_none():
    result = behavior([{'tag': 'stop', 'status': 'TRIGGERED'}])
    assert result['receipt_coverage'] is None
    assert result['insufficient_data'] is False
    assert result['discipline_rate'] == 0.0


def test_behavior_marks_low_coverage_as_insufficient():
    rows = [
        {'tag': 'stop', 'status': 'TRIGGERED', 'verdict': 'executed'},
        {'tag': 'stop', 'status': 'TRIGGERED', 'verdict': None},
        {'tag': 'stop', 'status': 'TRIGGERED', 'verdict': None},
    ]
    result = behavior(rows)
    assert result['receipt_coverage'] == pytest.approx(1 / 3)
    assert result['insufficient_data'] is True
    assert result['discipline_rate'] is None
