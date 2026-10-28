import json
import json
from datetime import UTC, datetime

import pytest

from pointtable.cli import build_report
from pointtable.db.session import (
    get_connection,
    init_db,
    save_report_signal_snapshot,
    save_journal,
    save_signal,
    set_database_path,
)
from pointtable.core.lifecycle import JournalEntry
from pointtable.jobs.evening import build_pool_signals, run_evening, signal_day_trigger_review
from pointtable.jobs.settle import run_settle
from pointtable.jobs.watch import run_watch
from pointtable.models.signal import Signal, SignalSide, SignalTag, SignalTrend, SignalValidPolicy, SignalStatus


def _signal(signal_id: str, zone_low: float, zone_high: float, created_at: datetime) -> Signal:
    return Signal(
        id=signal_id,
        symbol='300001',
        strategy_id='pool',
        side=SignalSide.SELL,
        tag=SignalTag.STOP,
        zone_low=zone_low,
        zone_high=zone_high,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        status=SignalStatus.TRIGGERED,
        parent_id='pool:300001',
        created_at=created_at,
        closed_at=created_at,
    )


def test_trigger_review_reads_trigger_time_snapshot(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    day_one = datetime(2026, 1, 1, 10, tzinfo=UTC)
    day_two = datetime(2026, 1, 2, 10, tzinfo=UTC)
    old_stop = _signal('2026-01-01-300001-stop', 10.0, 10.2, day_one)
    save_signal(conn, old_stop)
    save_journal(conn, JournalEntry(
        signal_id=old_stop.id,
        from_status='ACTIVE',
        to_status='TRIGGERED',
        reason='watch_hit',
        price=10.1,
        created_at=day_one,
    ))

    conn.execute(
        'UPDATE signals SET zone_low=?, zone_high=?, closed_at=? WHERE id=?',
        (30.0, 30.2, day_two.isoformat(timespec='seconds'), old_stop.id),
    )
    new_stop = _signal('2026-01-02-300001-stop', 20.0, 20.2, day_two)
    save_signal(conn, new_stop)
    save_journal(conn, JournalEntry(
        signal_id=new_stop.id,
        from_status='ACTIVE',
        to_status='TRIGGERED',
        reason='watch_hit',
        price=20.1,
        created_at=day_two,
    ))
    conn.commit()

    rows = signal_day_trigger_review(conn, '2026-01-01')
    assert len(rows) == 1
    assert rows[0]['signal_id'] == old_stop.id
    assert (rows[0]['zone_low'], rows[0]['zone_high']) == (10.0, 10.2)


def test_old_triggered_stop_does_not_suppress_next_day_pool_t():
    created_at = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    prior_stop = _signal('2026-01-01-300001-stop', 9.0, 9.2, datetime(2026, 1, 1, 15, 30, tzinfo=UTC))
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (10.0, 10.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.4, 10.6), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    signals = build_pool_signals([row], created_at, [prior_stop])
    assert [signal.tag for signal in signals if signal.tag is SignalTag.T] == [SignalTag.T, SignalTag.T]


def test_same_day_recovery_records_settlement_without_changing_qualifier(tmp_db):
    set_database_path(tmp_db)
    init_db()
    now = datetime(2026, 1, 1, 15, 5, tzinfo=UTC)
    stop = _signal('2026-01-01-300001-stop', 10.0, 10.2, now).model_copy(
        update={'break_qualifier': 'DELAY_OBSERVE'}
    )
    save_signal(get_connection(), stop)
    run_settle(
        get_connection(), now,
        close_prices={'300001': 10.5},
    )
    row = get_connection().execute(
        'SELECT break_qualifier, settlement_outcome FROM signals WHERE id=?',
        (stop.id,),
    ).fetchone()
    assert row['break_qualifier'] == 'DELAY_OBSERVE'
    assert row['settlement_outcome'] == 'SAME_DAY_RECOVERED'


def test_report_snapshot_replay_survives_later_signal_mutation(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    created_at = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    stop = _signal('2026-01-01-300001-stop', 10.0, 10.2, created_at)
    save_signal(conn, stop)
    run_watch(conn, 10.0, False, day_one, prices={'300001': 10.1})
    input_path = tmp_path / 'evening.json'
    input_path.write_text(json.dumps({'trade_date': '2026-01-01'}), encoding='utf-8')
    run_evening(conn, input_path, '2026-01-01')
    save_report_signal_snapshot(conn, '2026-01-01', [stop])
    market = {
        'trade_date': '2026-01-01',
        'name_map': {'300001': '测试'},
        'spot_prices': {'300001': 10.1},
    }
    first_report = build_report('2026-01-01', market)

    conn.execute(
        'UPDATE signals SET zone_low=?, zone_high=?, status=?, closed_at=NULL WHERE id=?',
        (30.0, 30.2, 'ACTIVE', stop.id),
    )
    conn.commit()

    replay_report = build_report('2026-01-01', market)
    assert replay_report == first_report
    assert first_report.count('10') > 0


def test_two_day_trigger_chain_keeps_a0_as_of_snapshot(tmp_db, tmp_path):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    day_one = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    stop = _signal(
        '2026-01-01-300001-stop', 10.0, 10.2,
        datetime(2026, 1, 1, 15, 30, tzinfo=UTC),
    ).model_copy(update={
        'status': SignalStatus.ACTIVE,
        'closed_at': None,
        'valid_policy': SignalValidPolicy.UNTIL_STOPPED,
    })
    save_signal(conn, stop)
    run_watch(conn, 10.0, False, day_one, prices={'300001': 10.1})
    input_path = tmp_path / 'evening.json'
    input_path.write_text(json.dumps({'trade_date': '2026-01-01'}), encoding='utf-8')
    run_evening(conn, input_path, '2026-01-01')
    conn.execute(
        'UPDATE signals SET zone_low=?, zone_high=? WHERE id=?', (30.0, 30.2, stop.id)
    )
    conn.commit()
    reviewed = signal_day_trigger_review(conn, '2026-01-01')
    assert (reviewed[0]['zone_low'], reviewed[0]['zone_high']) == (10.0, 10.2)
    assert build_report('2026-01-02', {'trade_date': '2026-01-02'}).count('10') > 0
