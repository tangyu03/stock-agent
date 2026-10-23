from datetime import UTC, datetime

from pointtable.core.generator import (
    Strategy,
    generate_entry,
    generate_holding_rows,
    order_for_push,
)
from pointtable.jobs.watch import watch_signals
from pointtable.models.signal import SignalStatus, SignalTag, SignalTrend


def test_replay_generates_and_orders_without_network():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='volume_breakout',
        side='BUY',
        premise='放量站上关键位',
        y={'type': 'pullback'},
        z={'type': 'close_below'},
        w={'type': 'next_resistance'},
    )
    entry_rows = generate_entry(
        strategy, '300001', (10.0, 10.2), (9.0, 9.2), (12.0, 12.2), now
    )
    holding_rows = generate_holding_rows(
        '300001', SignalTrend.UP, (9.0, 9.2), (10.0, 10.2), (11.0, 11.2),
        entry_rows[0].id, now,
    )
    assert [row.id for row in entry_rows] == [
        '2026-01-02-300001-volume_breakout',
        '2026-01-02-300001-volume_breakout-z',
        '2026-01-02-300001-volume_breakout-w',
    ]

    replay = order_for_push(entry_rows + holding_rows)
    assert [row.tag for row in replay] == [
        SignalTag.STOP,
        SignalTag.STOP,
        SignalTag.PROFIT,
        SignalTag.PROFIT,
        SignalTag.T,
        SignalTag.T,
        SignalTag.ENTER,
    ]

    entry_rows[0] = entry_rows[0].model_copy(update={'status': SignalStatus.TRIGGERED})
    active_only = watch_signals(entry_rows + holding_rows, 10.1, True)
    assert all(row.status is SignalStatus.ACTIVE for row in active_only)
    assert active_only[0].tag is SignalTag.STOP
