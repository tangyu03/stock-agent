import pytest

from pointtable.stats import behavior, signal_quality, signal_reachability


def test_signal_quality_uses_triggered_rows_and_reachability():
    rows = [
        {'strategy_id': 'a', 'status': 'TRIGGERED', 'confirmed': True, 'hypothetical_pnl_pct': 2.0},
        {'strategy_id': 'a', 'status': 'TRIGGERED', 'confirmed': False, 'hypothetical_pnl_pct': 100.0},
        {'strategy_id': 'a', 'status': 'ACTIVE', 'confirmed': False},
    ]
    market_rows = [{'symbol': '300001', 'est_close': 10.2, 'low': 10.0, 'high': 10.3}]
    quality_rows = [
        {**row, 'symbol': '300001', 'tag': 'enter', 'role': 'entry', 'zone_low': 10.0, 'zone_high': 10.2}
        for row in rows
    ]
    report = signal_quality(quality_rows, market_rows)['a']
    assert report['mechanical_expectancy'] == 51.0
    assert report['reachability']['reachable'] == 3
    assert not report['ev_negative']


def test_reachability_marks_tail_miss_unreachable():
    rows = [
        {'symbol': '300001', 'strategy_id': 'a', 'tag': 'enter', 'role': 'entry',
         'zone_low': 10.0, 'zone_high': 10.2, 'status': 'TRIGGERED'},
    ]
    market_rows = [{'symbol': '300001', 'est_close': 9.5, 'low': 9.8, 'high': 10.1}]
    report = signal_reachability(rows, market_rows)['a']
    assert report['reachable'] == 0
    assert report['daily_reachable'] == 1
    assert report['unreachable'] == 1


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
