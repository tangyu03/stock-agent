import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pointtable.cli import build_report
from pointtable.db.session import (
    get_connection,
    init_db,
    load_signals,
    save_report_signal_snapshot,
    save_journal,
    save_signal,
    save_stop_settlement,
    set_database_path,
)
from pointtable.core.lifecycle import JournalEntry
from pointtable.jobs.evening import build_pool_signals, run_evening, signal_day_trigger_review
from pointtable.jobs.settle import run_settle
from pointtable.jobs.settle import observation_pause_symbols, pool_protection_states
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


def test_report_reads_stop_settlement_fact_not_mutable_signal_row(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    created_at = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    stop = _signal('2026-01-01-300001-stop', 10.0, 10.2, created_at).model_copy(
        update={'break_qualifier': 'DELAY_OBSERVE'}
    )
    save_signal(conn, stop)
    save_report_signal_snapshot(conn, '2026-01-01', [stop])
    save_stop_settlement(
        conn,
        signal_id=stop.id,
        trade_date='2026-01-01',
        raw_qualifier='DELAY_OBSERVE',
        effective_qualifier='EXECUTE_NOW',
        circuit_day=True,
        close_price=9.9,
        zone_low=stop.zone_low,
        zone_high=stop.zone_high,
        settlement_outcome='EXECUTE_NOW',
        occurred_at=created_at,
    )
    market = {
        'trade_date': '2026-01-01',
        'name_map': {'300001': '测试'},
        'spot_prices': {'300001': 9.9},
    }
    report = build_report('2026-01-01', market)
    assert '原始定性：延迟观察' in report
    assert '有效定性：立即执行' in report
    assert '熔断日：是' in report
    assert '收盘价 9.9' in report
    assert '收盘结果：当日执行' in report


def test_two_day_trigger_chain_keeps_a0_as_of_snapshot(tmp_db):
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
    input_path = Path('test-two-day-evening.json')
    input_path.write_text(json.dumps({'trade_date': '2026-01-01'}), encoding='utf-8')
    run_evening(conn, input_path, '2026-01-01')
    conn.execute(
        'UPDATE signals SET zone_low=?, zone_high=? WHERE id=?', (30.0, 30.2, stop.id)
    )
    conn.commit()
    reviewed = signal_day_trigger_review(conn, '2026-01-01')
    assert (reviewed[0]['zone_low'], reviewed[0]['zone_high']) == (10.0, 10.2)
    assert build_report('2026-01-02', {'trade_date': '2026-01-01'}).count('10') > 0


def test_t_is_next_session_valid_and_paired(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    input_path = Path('test-t-validity.json')
    input_path.write_text(json.dumps({
        'trade_date': '2026-01-01',
        'signal_rows': [{
            'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
            'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
            't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
            'parent_id': 'pool:300001',
        }],
    }), encoding='utf-8')
    run_evening(conn, input_path, '2026-01-02')
    t_rows = [
        signal for signal in load_signals(conn)
        if signal.signal_role.value == 't'
    ]
    assert [signal.side.value for signal in t_rows] == ['BUY', 'SELL']
    assert all(signal.valid_for is not None and signal.valid_for.isoformat() == '2026-01-02' for signal in t_rows)
    assert len({signal.pair_id for signal in t_rows}) == 1

    run_watch(conn, 10.1, False, datetime(2026, 1, 1, 14, 0, tzinfo=UTC), prices={'300001': 10.1})
    assert all(signal.status.value == 'ACTIVE' for signal in load_signals(conn) if signal.signal_role.value == 't')
    run_watch(conn, 10.1, False, datetime(2026, 1, 2, 14, 0, tzinfo=UTC), prices={'300001': 10.1})
    assert any(signal.status.value == 'TRIGGERED' for signal in load_signals(conn) if signal.signal_role.value == 't')


def test_circuit_day_recovered_close_still_executes(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal('2026-01-01-300001-stop', 10.0, 10.2, datetime(2026, 1, 1, 10, tzinfo=UTC)).model_copy(
        update={'break_qualifier': 'DELAY_OBSERVE'}
    )
    save_signal(conn, stop)
    run_settle(
        conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        close_prices={'300001': 10.1},
        market_tier='LOW', index_trend='DOWN',
    )
    row = conn.execute(
        'SELECT settlement_outcome FROM signals WHERE id=?', (stop.id,)
    ).fetchone()
    assert row['settlement_outcome'] == 'EXECUTE_NOW'
    assert conn.execute('SELECT COUNT(*) AS n FROM correction_events').fetchone()['n'] == 0
    assert conn.execute('SELECT circuit_day FROM market_days').fetchone()['circuit_day'] == 1


def test_protection_pause_distinguishes_retest_terminal_states(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    confirmed = _signal(
        'retest-confirmed', 10.0, 10.2, datetime(2026, 1, 1, 10, tzinfo=UTC)
    ).model_copy(update={
        'tag': SignalTag.RETEST_CONFIRM,
        'side': SignalSide.NEUTRAL,
        'status': SignalStatus.TRIGGERED,
    })
    invalidated = confirmed.model_copy(update={
        'id': 'retest-invalidated',
        'symbol': '300002',
        'status': SignalStatus.INVALIDATED,
        'closed_at': datetime(2026, 1, 1, 10, tzinfo=UTC),
    })
    save_signal(conn, confirmed)
    save_signal(conn, invalidated)
    paused = observation_pause_symbols(conn)
    assert '300001' in paused
    assert '300002' not in paused


def test_pool_protection_states_matrix(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    created_at = datetime(2026, 1, 1, 10, tzinfo=UTC)
    rows = [
        _signal('retest-active', 10, 10.2, created_at).model_copy(update={
            'symbol': '300001', 'tag': SignalTag.RETEST_CONFIRM,
            'side': SignalSide.NEUTRAL, 'status': SignalStatus.ACTIVE,
        }),
        _signal('retest-expired', 10, 10.2, created_at).model_copy(update={
            'symbol': '300002', 'tag': SignalTag.RETEST_CONFIRM,
            'side': SignalSide.NEUTRAL, 'status': SignalStatus.EXPIRED,
        }),
        _signal('retest-invalidated', 10, 10.2, created_at).model_copy(update={
            'symbol': '300003', 'tag': SignalTag.RETEST_CONFIRM,
            'side': SignalSide.NEUTRAL, 'status': SignalStatus.INVALIDATED,
        }),
        _signal('300004-stop', 10, 10.2, created_at).model_copy(update={
            'symbol': '300004',
        }),
        _signal('300005-stop', 10, 10.2, created_at).model_copy(update={
            'symbol': '300005',
        }),
    ]
    for signal in rows:
        save_signal(conn, signal)
    save_stop_settlement(
        conn, signal_id='300004-stop', trade_date='2026-01-01',
        raw_qualifier='DELAY_OBSERVE', effective_qualifier='EXECUTE_NOW',
        circuit_day=True, close_price=9.8, zone_low=10, zone_high=10.2,
        settlement_outcome='EXECUTE_NOW', occurred_at=created_at,
    )
    save_stop_settlement(
        conn, signal_id='300005-stop', trade_date='2026-01-01',
        raw_qualifier='LIMIT_EXEMPT', effective_qualifier='LIMIT_EXEMPT',
        circuit_day=False, close_price=10.1, zone_low=10, zone_high=10.2,
        settlement_outcome='LIMIT_EXEMPT', occurred_at=created_at,
    )
    states = pool_protection_states(conn)
    assert states['300001'] == ('BLOCKED', 'retest_active')
    assert states['300002'] == ('WAIT_REENTRY', 'retest_expired')
    assert states['300003'] == ('NORMAL', 'retest_invalidated')
    assert states['300004'] == ('WAIT_REENTRY', 'execute_now')
    assert states['300005'] == ('NORMAL', 'limit_exempt')
