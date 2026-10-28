from datetime import UTC, datetime

import pytest

from pointtable.core.generator import (
    Strategy,
    generate_entry,
    load_strategies,
    order_for_push,
    premise_ok,
    validate_strategy,
)
from pointtable.core.selector import select_strategies
from pointtable.jobs.evening import build_evening_signals, build_pool_signals
from pointtable.models.signal import SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _strategy() -> Strategy:
    return Strategy(
        id='volume_breakout',
        side='BUY',
        premise='放量站上关键位',
        y={'type': 'pullback'},
        z={'type': 'close_below'},
        w={'type': 'next_resistance'},
    )


def test_strategy_file_loads_and_validates_four_keys():
    strategies = load_strategies('config/strategies.yaml')
    assert [item.id for item in strategies] == [
        'first_move_5d', 'first_move_10d', 'first_move_drift',
        'first_board_dip', 'volume_breakout',
    ]


def test_enter_generates_parent_z_and_w():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    rows = generate_entry(
        _strategy(), '300001', (10.0, 10.2), now
    )
    assert [row.tag for row in rows] == [SignalTag.ENTER]
    assert rows[0].valid_policy is SignalValidPolicy.NEXT_DAY
    assert rows[0].premise['premise'] == _strategy().premise


def test_entry_tail_snapshot_is_windowed():
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='热门题材',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )
    row = generate_entry(strategy, '300001', (10.0, 10.2), datetime(2026, 1, 2, tzinfo=UTC))[0]
    assert row.trigger_window_start is not None
    assert row.trigger_window_end is not None


def test_invalid_strategy_is_rejected():
    with pytest.raises(ValueError, match='w'):
        validate_strategy(Strategy('bad', 'BUY', '放量', {'type': 'a'}, {'type': 'b'}, {}))


def test_missing_premise_is_rejected():
    with pytest.raises(ValueError, match='premise'):
        validate_strategy(Strategy('bad', 'BUY', '', {'type': 'a'}, {'type': 'b'}, {'type': 'c'}))


def test_pool_signals_follow_trend_rules():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    up = build_pool_signals([row], now)
    down = build_pool_signals([dict(row, trend='DOWN')], now)
    assert [row.tag for row in up] == [SignalTag.STOP, SignalTag.PROFIT, SignalTag.T, SignalTag.T]
    assert [row.tag for row in down] == [SignalTag.STOP, SignalTag.PROFIT]


def test_pool_signal_trend_updates_existing_rows():
    prior = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    existing = build_pool_signals([row], prior)
    rows = build_pool_signals([dict(row, trend='DOWN')], now, existing)
    stop = next(signal for signal in rows if signal.tag is SignalTag.STOP and signal.status is SignalStatus.ACTIVE)
    profit = next(signal for signal in rows if signal.tag is SignalTag.PROFIT and signal.status is SignalStatus.ACTIVE)
    assert stop.trend is SignalTrend.DOWN
    assert profit.trend is SignalTrend.DOWN
    assert all(signal.status is SignalStatus.EXPIRED for signal in rows if signal.tag is SignalTag.T)


def test_pool_t_buy_is_suppressed_by_triggered_or_overlapping_stop():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    triggered_stop = build_pool_signals([row], now)[0].model_copy(update={
        'status': SignalStatus.TRIGGERED,
        'closed_at': now,
    })
    assert [
        signal.tag for signal in build_pool_signals([row], now, [triggered_stop])
        if signal.tag is SignalTag.T
    ] == []
    conflicting = dict(row, t_buy_zone=(9.1, 9.3))
    assert [
        signal.tag for signal in build_pool_signals([conflicting], now)
        if signal.tag is SignalTag.T
    ] == []
    wide = dict(row, t_buy_zone=(10.0, 12.0))
    assert [
        signal.tag for signal in build_pool_signals([wide], now)
        if signal.tag is SignalTag.T
    ] == []
    unordered = dict(row, t_sell_zone=(10.1, 10.3))
    assert [
        signal.tag for signal in build_pool_signals([unordered], now)
        if signal.tag is SignalTag.T
    ] == []


def test_pool_signal_updates_strategy_and_same_day_trend():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    old_stop = build_pool_signals([{
        'symbol': '300001', 'strategy_id': 'holding', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }], now)
    rows = build_pool_signals([{
        'symbol': '300001', 'strategy_id': 'first_move_5d', 'trend': 'UP',
        'stop_zone': (9.1, 9.3), 'profit_zone': (11.1, 11.3),
        't_buy_zone': (10.1, 10.3), 't_sell_zone': (11.1, 11.3),
        'parent_id': 'pool:300001',
    }], now, old_stop)
    assert all(signal.strategy_id == 'first_move_5d' for signal in rows if signal.status is SignalStatus.ACTIVE)


def test_evening_does_not_reset_triggered_entry():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='热门题材 + 5日线上行',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )
    plan = {
        'symbol': '300001', 'strategy_id': 'first_move_5d',
        'entry_low': 10.0, 'entry_high': 10.2,
        'invalidation_price': 10.1, 'invalidation_note': '5日线',
        'rationale': '5日线', 'parent_id': None,
        'premise_facts': {'hot_theme': True, 'not_regulated': True, 'lhb_ok': True},
    }
    generated = build_evening_signals({'first_move_5d': strategy}, [plan], 'LOW', now)
    triggered = [signal.model_copy(update={
        'status': SignalStatus.TRIGGERED,
        'closed_at': now,
    }) for signal in generated]
    assert build_evening_signals(
        {'first_move_5d': strategy}, [plan], 'LOW', now, triggered
    ) == []


def test_volume_breakout_does_not_require_hot_theme():
    facts = {
        'not_regulated': True, 'lhb_ok': True, 'volume_breakout': True,
    }
    assert select_strategies(facts) == ['volume_breakout']


def test_same_tick_ordering_puts_stop_first():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    rows = build_pool_signals(
        [{
            'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
            'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
            't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
            'parent_id': 'pool:300001',
        }],
        now,
    )
    assert order_for_push(rows)[0].tag is SignalTag.STOP
    assert premise_ok({'hot_theme': True, 'not_regulated': True})
    assert not premise_ok({'hot_theme': True, 'not_regulated': False})
