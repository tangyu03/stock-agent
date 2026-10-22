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
    assert result['discipline_rate'] == 0.0
