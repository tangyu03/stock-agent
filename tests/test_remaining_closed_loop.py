import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pointtable.core.volume import BreakQualifier
from pointtable.db.session import (
    get_connection,
    init_db,
    load_market_snapshots,
    save_journal,
    save_market_snapshots,
    save_signals,
    set_database_path,
)
from pointtable.stats import pool_performance_report, five_day_performance
from pointtable.jobs.restore_check import find_unpaired_t_buy
from pointtable.jobs.evening import run_evening
from pointtable.db.session import signal_from_row
from pointtable.jobs.settle import run_settle
from pointtable.jobs.watch import run_watch
from pointtable.cli import stats_report
from pointtable.jobs.watch import stop_break_decision
from pointtable.models.journal import JournalEntry
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
    assert stop_break_decision({'volume_ratio': 1.5})[0] is BreakQualifier.UNCLASSIFIED
    assert stop_break_decision({'volume_ratio': 1.5})[1] == 'active_delta_missing'
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


def test_watch_loads_next_day_entry_created_on_prior_day(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    signal = Signal(
        id='2026-01-01-300001-entry',
        symbol='300001',
        strategy_id='volume_breakout',
        side=SignalSide.BUY,
        tag=SignalTag.ENTER,
        zone_low=10.0,
        zone_high=10.2,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.NEXT_DAY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_signals(conn, [signal])
    result = run_watch(
        conn, 10.1, False, datetime(2026, 1, 2, tzinfo=UTC), trade_date='2026-01-02',
    )
    assert result['touched'] == [signal.id]


def test_find_unpaired_t_buy_rules(tmp_db):
    buy = _signal()
    sell = _signal('2026-01-01-300001-t-sell', 'SELL')
    unpaired, violations = find_unpaired_t_buy([buy, sell])
    assert unpaired == []
    assert violations == []


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
    assert report['stop_tracking'] == [{
        'signal_id': '2026-01-01-300001-stop',
        'zone_low': 9.0,
        'zone_high': 11.0,
        'trigger_time': '2026-01-01T00:00:00+00:00',
        'sell_fly': False,
        'loss_avoided': True,
    }]


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
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))
    conn.commit()
    # §9 假想期望输入：成交日收盘 10.0，其后 5 个快照日收盘 9.0 → 期望为负
    for day_index in range(6):
        save_market_snapshots(conn, f'2026-01-0{day_index + 1}', [{
            'symbol': '300001',
            'est_close': 10.0 if day_index == 0 else 9.0,
            'low': 9.0,
            'high': 10.5,
            'volume_ratio': 1.0,
        }])
    payload = {
        'trade_date': '2026-01-01',
        'created_at': '2026-01-01T15:30:00+08:00',
            'signal_rows': [{
                'symbol': '300001',
                'strategy_id': 'holding',
                'parent_id': 'holding-1',
                'stop_zone': [9.0, 9.2],
                'profit_zone': [10.8, 11.0],
                't_buy_zone': [9.5, 9.7],
                't_sell_zone': [10.8, 11.0],
            'trend': 'UP',
        }],
    }
    payload_path = Path.cwd() / 'test_evening_payload.json'
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    result = run_evening(conn, payload_path)
    payload_path.unlink(missing_ok=True)
    saved = signal_from_row(conn.execute(
        "SELECT * FROM signals WHERE tag='t' AND side='BUY' ORDER BY id DESC LIMIT 1",
    ).fetchone())
    assert saved.premise['ev_negative'] is True


def test_evening_persists_market_snapshot_and_stats_uses_reachability(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    payload = {
        'trade_date': '2026-01-01',
        'created_at': '2026-01-01T15:30:00+08:00',
        'assignments': [{
            'symbol': '300001',
            'strategy_id': 'first_move_5d',
            'closes': [10.0, 10.0, 10.0, 10.0],
            'est_close': 10.2,
            'low': 10.0,
            'high': 10.5,
            'volume_ratio': 1.5,
            'premise_facts': {'hot': True, 'not_regulated': True},
        }],
    }
    payload_path = Path.cwd() / 'test_evening_market_snapshot.json'
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    try:
        run_evening(conn, payload_path)
        rows = load_market_snapshots(conn, '2026-01-01')
        assert rows == [{
            'trade_date': '2026-01-01',
            'symbol': '300001',
            'est_close': 10.2,
            'low': 10.0,
            'high': 10.5,
            'volume_ratio': 1.5,
            'two_source_anomaly': False,
        }]
        report = stats_report(date='2026-01-01')
        first_move = report['signal_quality']['first_move_5d']
        assert first_move['reachability']['audited'] >= 1
        assert first_move['reachability']['daily_reachable'] >= 1
    finally:
        payload_path.unlink(missing_ok=True)


def test_zone_snapshots_persist_initial_and_rolled_stop(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _stop()
    save_signals(conn, [stop])
    initial = conn.execute(
        'SELECT zone_low, zone_high FROM zone_snapshots WHERE signal_id=?',
        (stop.id,),
    ).fetchall()
    assert [(row['zone_low'], row['zone_high']) for row in initial] == [(9.0, 11.0)]
    now = datetime(2026, 1, 2, tzinfo=UTC)
    run_settle(conn, now, stop_zones={'300001': (9.5, 9.8)})
    rolled = conn.execute(
        'SELECT zone_low, zone_high FROM zone_snapshots WHERE signal_id=? ORDER BY effective_at',
        (stop.id,),
    ).fetchall()
    assert [(row['zone_low'], row['zone_high']) for row in rolled] == [(9.0, 11.0), (9.5, 9.8)]
