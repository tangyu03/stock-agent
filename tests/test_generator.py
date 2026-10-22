from datetime import UTC, datetime

import pytest

from pointtable.core.generator import (
    Strategy,
    generate_entry,
    generate_holding_rows,
    load_strategies,
    order_for_push,
    premise_ok,
    validate_strategy,
)
from pointtable.models.signal import SignalTag, SignalTrend


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
    strategies = load_strategies('pointtable/config/strategies.yaml')
    assert [item.id for item in strategies] == [
        'first_move_5d', 'first_move_10d', 'first_move_drift',
        'first_board_dip', 'volume_breakout',
    ]


def test_enter_generates_parent_z_and_w():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    rows = generate_entry(
        _strategy(), '300001', (10.0, 10.2), (9.0, 9.2), (12.0, 12.2), now
    )
    assert [row.tag for row in rows] == [SignalTag.ENTER, SignalTag.STOP, SignalTag.PROFIT]
    assert rows[1].parent_id == rows[0].id
    assert rows[2].parent_id == rows[0].id


def test_invalid_strategy_is_rejected():
    with pytest.raises(ValueError, match='w'):
        validate_strategy(Strategy('bad', 'BUY', '', {'type': 'a'}, {'type': 'b'}, {}))


def test_holding_rows_follow_trend_rules():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    up = generate_holding_rows(
        '300001', SignalTrend.UP, (9.0, 9.2), (10.0, 10.2), (11.0, 11.2), 'evt-1', now
    )
    down = generate_holding_rows(
        '300001', SignalTrend.DOWN, (9.0, 9.2), (10.0, 10.2), (11.0, 11.2), 'evt-1', now
    )
    assert [row.tag for row in up] == [SignalTag.STOP, SignalTag.T, SignalTag.T]
    assert [row.tag for row in down] == [SignalTag.STOP]


def test_same_tick_ordering_puts_stop_first():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    rows = generate_holding_rows(
        '300001', SignalTrend.UP, (9.0, 9.2), (10.0, 10.2), (11.0, 11.2), 'evt-1', now
    )
    assert order_for_push(rows)[0].tag is SignalTag.STOP
    assert premise_ok({'hot_theme': True, 'not_regulated': True})
    assert not premise_ok({'hot_theme': True, 'not_regulated': False})
