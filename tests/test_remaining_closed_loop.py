import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pointtable.core.volume import BreakQualifier
from pointtable.db.session import get_connection, init_db, save_journal, save_signals, set_database_path
from pointtable.jobs.performance import pool_performance_report, five_day_performance
from pointtable.jobs.restore_check import find_unpaired_t_buy
from pointtable.jobs.routines import run_evening, run_watch, signal_from_row
from pointtable.jobs.watch import stop_break_decision
from pointtable.models.journal import JournalEntry, TransitionConfirmation
from pointtable.models.signal import Signal, SignalSide, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _signal(signal_id='2026-01-01-300001-t-buy', side='BUY', parent_id='p-1'):
    return Signal(
        id=signal_id,
        symbol='300001',
        strategy_id='holding',
        side=SignalSide(side),
        tag=SignalTag.T,
        zone_low=10.0,
        zone_high=10.8,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.INTRADAY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        parent_id=parent_id,
    )


def _stop(parent_id='p-1', signal_id='2026-01-01-300001-stop'):
    return Signal(
        id=signal_id,
        symbol='300001',
        strategy_id='s',
        side=SignalSide.SELL,
        tag=SignalTag.STOP,
        zone_low=9.0,
        zone_high=11.0,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        parent_id=parent_id,
    )


def test_stop_break_decision_and_context_does_not_change_transition(tmp_db):
    active_context = {
        'volume_ratio': 1.5,
        'active_delta': 1000,
        'stock_triggered': True,
        'sector_triggered': False,
        'index_triggered': False,
    }
    passive_context = {
        'volume_ratio': 0.7,
        'active_delta': 0,
        'stock_triggered': True,
        'sector_triggered': True,
        'index_triggered': False,
    }
    assert stop_break_decision(active_context)[0] is BreakQualifier.EXECUTE_NOW
    assert stop_break_decision(passive_context)[0] is BreakQualifier.DELAY_OBSERVE
    assert stop_break_decision(None)[1] == 'NOT_CLASSIFIED'

    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    signal = _stop()
    save_signals(conn, [signal])
    result = run_watch(
        conn,
        10.5,
        True,
        datetime(2026, 1, 1, tzinfo=UTC),
        trade_date='2026-01-01',
        break_context={'300001': passive_context},
    )
    assert result['touched'] == [signal.id]
    assert result['break_decisions'][0]['qualifier'] == 'DELAY_OBSERVE'
    row = signal_from_row(conn.execute('SELECT * FROM signals').fetchone())
    assert row.status.value == 'TRIGGERED'
    assert 'DELAY_OBSERVE' in row.status_reason


def test_find_unpaired_t_buy_rules(tmp_db):
    buy = _signal()
    sell = _signal('2026-01-01-300001-t-sell', 'SELL')
    unpaired, no_receipt, violations = find_unpaired_t_buy([buy, sell], [])
    assert unpaired == []
    assert no_receipt == [buy.id]
    assert violations == []

    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    conn.execute(
        'INSERT INTO receipts (signal_id, verdict, created_at) VALUES (?,?,?)',
        (buy.id, 'executed', buy.created_at.isoformat(timespec='seconds')),
    )
    receipt = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts').fetchall()]
    unpaired, no_receipt, violations = find_unpaired_t_buy([buy, sell], receipt)
    assert [item.signal_id for item in unpaired] == [buy.id]
    assert no_receipt == []
    assert violations == [buy.id]

    conn.execute(
        'INSERT INTO receipts (signal_id, verdict, created_at) VALUES (?,?,?)',
        (sell.id, 'executed', sell.created_at.isoformat(timespec='seconds')),
    )
    receipt = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts').fetchall()]
    unpaired, no_receipt, violations = find_unpaired_t_buy([buy, sell], receipt)
    assert (unpaired, no_receipt, violations) == ([], [], [])


def test_five_day_performance_and_pool_report(tmp_db):
    rows = [
        {'trade_date': '2026-01-01', 'close': 10.0},
        {'trade_date': '2026-01-02', 'close': 10.1},
        {'trade_date': '2026-01-03', 'close': 10.2},
        {'trade_date': '2026-01-04', 'close': 10.3},
        {'trade_date': '2026-01-05', 'close': 10.4},
        {'trade_date': '2026-01-06', 'close': 10.5},
    ]
    assert five_day_performance('300001', '2026-01-01', rows)['five_day_return'] == pytest.approx(0.05)
    assert five_day_performance('300001', '2026-01-06', rows) is None

    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    conn.execute(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        ('2026-01-01', '300001', 'AI'),
    )
    stop = _stop(signal_id='2026-01-01-300001-stop')
    save_signals(conn, [stop])
    save_journal(conn, JournalEntry(
        signal_id=stop.id,
        from_status='ACTIVE',
        to_status='TRIGGERED',
        reason='stop_single_point',
        price=10.5,
        confirmed=TransitionConfirmation.TOUCHED_ONLY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    conn.commit()
    report = pool_performance_report(
        conn,
        '2026-01-01',
        stock_klines={'300001': rows},
    )
    assert report['performance'][0]['symbol'] == '300001'
    assert report['pool_quality']['measured'] == 1
    assert report['correction_hit_rate'] == 0.0


def test_run_evening_marks_ev_negative_strategy(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    rows = []
    for index in range(30):
        signal = _signal(
            f'2026-01-01-{index:03d}-enter',
            parent_id=f'old-{index}',
        ).model_copy(update={'status': SignalStatus.TRIGGERED})
        rows.append(signal)
    save_signals(conn, rows)
    for signal in rows:
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status='ACTIVE',
            to_status='TRIGGERED',
            reason='buy_confirmed',
            price=10.5,
            confirmed=TransitionConfirmation.CONFIRMED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))
    conn.commit()
    payload = {
        'trade_date': '2026-01-01',
        'created_at': '2026-01-01T15:30:00+08:00',
        'holdings': [{
            'symbol': '300001',
            'parent_id': 'holding-1',
            'stop_zone': [9.0, 9.2],
            'support_zone': [9.5, 9.7],
            'resistance_zone': [10.8, 11.0],
            'trend': 'UP',
        }],
    }
    payload_path = Path.cwd() / 'test_evening_payload.json'
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    result = run_evening(conn, payload_path)
    payload_path.unlink(missing_ok=True)
    saved = signal_from_row(conn.execute(
        "SELECT * FROM signals WHERE id='2026-01-01-300001-t-buy'",
    ).fetchone())
    assert saved.premise['ev_negative'] is True
