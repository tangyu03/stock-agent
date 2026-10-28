import json
import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path

from pointtable.cli import build_report
from pointtable.core.generator import Strategy, generate_entry
from pointtable.db.session import (
    get_connection,
    init_db,
    load_context_marks,
    save_context_marks,
    save_signals,
    save_correction_anchor,
    set_database_path,
)
from pointtable.jobs.evening import build_context_marks
from pointtable.jobs.evening import build_event_calendar
from pointtable.jobs.evening import build_pool_signals, run_evening
from pointtable.jobs.settle import run_settle
from pointtable.jobs.watch import run_watch
from pointtable.jobs.watch import context_from_marks
from pointtable.jobs.watch import process_touch
from pointtable.models.context_mark import ContextType
from pointtable.models.signal import Signal, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _signal(tag=SignalTag.ENTER, status=SignalStatus.ACTIVE, **kwargs):
    return Signal(
        id=kwargs.get('id', 's-1'),
        symbol=kwargs.get('symbol', '300001'),
        strategy_id=kwargs.get('strategy_id', 'first_move_5d'),
        side=kwargs.get('side', 'BUY'),
        tag=tag,
        zone_low=kwargs.get('zone_low', 10.0),
        zone_high=kwargs.get('zone_high', 10.2),
        trend=SignalTrend.UP,
        valid_policy=kwargs.get('valid_policy', SignalValidPolicy.INTRADAY),
        status=status,
        created_at=kwargs.get('created_at', datetime(2026, 1, 1, tzinfo=UTC)),
        closed_at=kwargs.get(
            'closed_at',
            datetime(2026, 1, 1, tzinfo=UTC)
            if status in {SignalStatus.TRIGGERED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
            else None,
        ),
        parent_id=kwargs.get('parent_id'),
        trigger_window_start=kwargs.get('trigger_window_start'),
        trigger_window_end=kwargs.get('trigger_window_end'),
        break_qualifier=kwargs.get('break_qualifier'),
    )


def test_ma_entry_window_is_hard():
    row = _signal(
        trigger_window_start=time(14, 30),
        trigger_window_end=time(15, 0),
    )
    outside, _, reason = process_touch(
        row, 10.1, now=datetime(2026, 1, 1, 10, 23, tzinfo=UTC)
    )
    assert outside is None and reason == 'outside_trigger_window'
    inside, journal, reason = process_touch(
        row, 10.1, now=datetime(2026, 1, 1, 14, 47, tzinfo=UTC)
    )
    assert reason == 'entry_single_point'
    assert inside.status is SignalStatus.TRIGGERED
    assert journal is not None


def test_entry_generates_only_entry():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='hot',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )
    rows = generate_entry(
        strategy, '300001', (10, 10.2), now,
    )
    assert rows[0].trigger_window_start == time(14, 30)
    assert len(rows) == 1


def test_active_retest_pauses_pool_signals(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    retest = _signal(
        SignalTag.RETEST_CONFIRM, side='NEUTRAL', id='retest-1',
        strategy_id='pool', parent_id='pool:300001',
        valid_policy=SignalValidPolicy.FIXED_WINDOW,
    )
    save_signals(conn, [retest])
    payload = {
        'trade_date': '2026-01-01',
        'signal_rows': [{
            'symbol': '300001', 'parent_id': 'pool:300001', 'trend': 'UP',
            'stop_zone': (9, 9.2), 'profit_zone': (11, 11.2),
            't_buy_zone': (10, 10.2), 't_sell_zone': (11, 11.2),
        }],
    }
    path = Path('test-v8-evening.json')
    path.write_text(json.dumps(payload), encoding='utf-8')
    result = run_evening(conn, path)
    assert result['signal_ids'] == []


def test_settle_reads_break_qualifier(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='DELAY_OBSERVE',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    result = run_settle(
        conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        close_prices={'300001': 10.1},
    )
    assert result['retest_created'] == []
    assert result['correction_notices']


def test_melt_day_treats_delay_as_execute_without_retest(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='DELAY_OBSERVE',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    result = run_settle(
        conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        close_prices={'300001': 9.1},
        market_tier='LOW', index_trend='DOWN',
    )
    assert result['retest_created'] == []
    anchor = conn.execute(
        'SELECT qualifier FROM correction_anchors WHERE signal_id=?',
        ('stop-1',),
    ).fetchone()
    assert anchor['qualifier'] == 'EXECUTE_NOW'


def test_delayed_break_requires_close_price(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='DELAY_OBSERVE',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    try:
        run_settle(conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC))
    except ValueError as error:
        assert 'missing close price' in str(error)
    else:
        raise AssertionError('missing close price must fail settlement')


def test_non_weak_close_break_creates_retest(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='DELAY_OBSERVE',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    result = run_settle(
        conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        close_prices={'300001': 9.1},
    )
    assert result['retest_created'] == ['stop-1-retest']
    assert result['correction_notices'] == []


def test_execute_now_correction_is_watch_only(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='EXECUTE_NOW',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    first = run_settle(
        conn, datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        close_prices={'300001': 9.1},
    )
    assert first['retest_created'] == []
    same_day = run_watch(
        conn, 10.1, False, datetime(2026, 1, 1, 15, 10, tzinfo=UTC),
        prices={'300001': 10.1},
    )
    assert same_day['correction_notices'] == []
    next_day = run_watch(
        conn, 10.1, False, datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
        prices={'300001': 10.1},
    )
    assert len(next_day['correction_notices']) == 1
    assert 'EXECUTE_NOW' in next_day['correction_notices'][0]
    repeat = run_watch(
        conn, 10.1, False, datetime(2026, 1, 2, 11, 0, tzinfo=UTC),
        prices={'300001': 10.1},
    )
    assert repeat['correction_notices'] == []


def test_same_day_close_correction_persists_once_and_renders(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='DELAY_OBSERVE',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    save_signals(conn, [stop])
    now = datetime(2026, 1, 1, 15, 5, tzinfo=UTC)
    first = run_settle(
        conn, now,
        close_prices={'300001': 10.1},
    )
    repeat = run_settle(
        conn, now,
        close_prices={'300001': 10.1},
    )
    assert first['correction_notices']
    assert repeat['correction_notices'] == []
    rows = conn.execute(
        'SELECT COUNT(*) AS n FROM correction_events WHERE signal_id=?',
        ('stop-1',),
    ).fetchone()
    assert rows['n'] == 1
    market = {
        'name_map': {'300001': '测试'},
        'spot_prices': {'300001': 10.1},
    }
    text = build_report('2026-01-01', market)
    assert '收盘处理（1条）' in text
    assert '测试（300001）' in text
    assert '当日收复' in text


def test_pool_stop_after_execute_now_is_suppressed_until_new_entry():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    executed_stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='pool',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='EXECUTE_NOW',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    assert build_pool_signals([row], now, [executed_stop]) == []

    new_entry = _signal(status=SignalStatus.TRIGGERED).model_copy(update={
        'created_at': now,
    })
    assert build_pool_signals([row], now, [executed_stop, new_entry])


def test_rebuilt_pool_stop_records_prior_stop_id():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        'symbol': '300001', 'strategy_id': 'pool', 'trend': 'UP',
        'stop_zone': (9.0, 9.2), 'profit_zone': (11.0, 11.2),
        't_buy_zone': (10.0, 10.2), 't_sell_zone': (11.0, 11.2),
        'parent_id': 'pool:300001',
    }
    prior_stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='pool',
        parent_id='pool:300001', status=SignalStatus.TRIGGERED,
        break_qualifier='SAME_DAY_RECOVERED',
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    stop = next(
        signal for signal in build_pool_signals([row], now, [prior_stop])
        if signal.tag is SignalTag.STOP and signal.status is SignalStatus.ACTIVE
    )
    assert stop.premise['prior_stop_id'] == 'stop-1'


def test_active_candidate_expires_on_same_ma_cooldown(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    candidate = _signal(strategy_id='first_move_5d')
    save_signals(conn, [candidate])
    input_path = Path('test-candidate-cooldown.json')
    input_path.write_text(json.dumps({
        'trade_date': '2026-01-01',
        'created_at': '2026-01-01T15:30:00+08:00',
        'same_ma_cooldown_days': 2,
        'recent_stop_counts': {'300001': {'ma5': 2}},
    }), encoding='utf-8')
    run_evening(conn, input_path)
    row = conn.execute(
        'SELECT status, status_reason FROM signals WHERE id=?',
        (candidate.id,),
    ).fetchone()
    assert row['status'] == 'EXPIRED'
    assert row['status_reason'] == 'ma_family_cooldown'
    input_path.unlink()


def test_correction_projection_merges_same_symbol_and_date(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    rows = []
    for signal_id, strategy_id in [
        ('stop-1', 'holding'), ('stop-2', 'first_move_5d')
    ]:
        stop = _signal(
            SignalTag.STOP, side='SELL', id=signal_id, strategy_id=strategy_id,
            parent_id='pool:300001', status=SignalStatus.TRIGGERED,
            valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        )
        rows.append(stop)
        save_correction_anchor(
            conn,
            signal_id=signal_id,
            symbol='300001',
            trade_date='2026-01-01',
            qualifier='EXECUTE_NOW',
            zone_low=10,
            zone_high=10.2,
            created_at=datetime(2026, 1, 1, 15, 5, tzinfo=UTC),
        )
    save_signals(conn, rows)
    market = {
        'name_map': {'300001': '测试'},
        'trade_date': '2026-01-01',
    }
    text = build_report('2026-01-01', market)
    assert '收盘处理（1条）' in text


def test_correction_anchor_schema_supports_one_per_trade_date(tmp_db):
    set_database_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.executescript('''
        CREATE TABLE correction_anchors (
            signal_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            qualifier TEXT NOT NULL,
            zone_low REAL NOT NULL,
            zone_high REAL NOT NULL,
            notice_price REAL,
            noticed_at TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    init_db()
    conn = get_connection()
    assert save_correction_anchor(
        conn, signal_id='stop-1', symbol='300001', trade_date='2026-01-01',
        qualifier='EXECUTE_NOW', zone_low=10, zone_high=10.2,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert save_correction_anchor(
        conn, signal_id='stop-1', symbol='300001', trade_date='2026-01-02',
        qualifier='EXECUTE_NOW', zone_low=10, zone_high=10.2,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert conn.execute('SELECT COUNT(*) AS n FROM correction_anchors').fetchone()['n'] == 2


def test_report_projects_lifecycle_slots(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    entry = _signal(id='entry-1')
    stop = _signal(
        SignalTag.STOP, side='SELL', id='stop-1', strategy_id='holding',
        parent_id='pool:300001', valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    retest = _signal(
        SignalTag.RETEST_CONFIRM, side='NEUTRAL', id='retest-1',
        strategy_id='holding', parent_id='pool:300001',
        valid_policy=SignalValidPolicy.FIXED_WINDOW,
    )
    save_signals(conn, [entry, stop, retest])
    marks = build_context_marks(
        [{'symbol': 'sh000001', 'zone': (3000, 3010), 'label': 'UP'}],
        [], date(2026, 1, 1), datetime(2026, 1, 1, 15, 30, tzinfo=UTC),
    )
    save_context_marks(conn, marks)
    market = {
        'assignments': [{'symbol': '300001', 'est_close': 10.1}],
        'amount_yi': 11000, 'tier': 'NORMAL', 'sh_close': 3005,
        'sh_ma5': 3000, 'index_trend': 'UP',
        'name_map': {'300001': '测试'},
        'spot_prices': {'300001': 10.2},
    }
    text = build_report('2026-01-01', market)
    for heading in ('候选（1只', '池内票区（1只）', '观察区（1个）'):
        assert heading in text
    assert '现价：10.2' in text


def test_watch_touches_context_marks(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    marks = build_context_marks(
        [{'symbol': 'sh000001', 'zone': (3000, 3010)}],
        [{'symbol': 'AI', 'zone': (100, 110)}],
        now.date(), now,
    )
    save_context_marks(conn, marks)
    run_watch(
        conn, 10.0, False, now,
        prices={'300001': 10.0},
        context_prices={'index': {'sh000001': 3005}, 'sector': {'AI': 105}},
    )
    rows = {row.code: row for row in load_context_marks(conn, now.date().isoformat())}
    assert rows['sh000001'].triggered_at is not None
    assert rows['AI'].triggered_at is not None


def test_context_fact_uses_any_same_day_trigger(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    triggered_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    marks = build_context_marks(
        [{'symbol': 'sh000001', 'zone': (10, 11)}],
        [{'symbol': 'AI', 'zone': (10, 11)}],
        now.date(), triggered_at,
    )
    conn.execute(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        (now.date().isoformat(), '300001', 'AI'),
    )
    save_context_marks(conn, [
        mark.model_copy(update={'triggered_at': triggered_at})
        for mark in marks
    ])
    assert context_from_marks(conn, '300001', now) == {
        'stock_triggered': False,
        'sector_triggered': True,
        'index_triggered': True,
    }


def test_stale_sector_context_does_not_trigger_break_fact(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    now = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    triggered_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    marks = build_context_marks(
        [],
        [{'symbol': 'AI', 'zone': (10, 11)}],
        now.date(), now,
    )
    conn.execute(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        (now.date().isoformat(), '300001', 'AI'),
    )
    save_context_marks(conn, [
        mark.model_copy(update={
            'label': 'stale_sector_context',
            'triggered_at': triggered_at,
        })
        for mark in marks
    ])
    context = context_from_marks(conn, '300001', now)
    assert context['sector_triggered'] is False


def test_tail_snapshot_plans_go_to_event_calendar():
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='hot',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )
    now = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    events = build_event_calendar(
        {strategy.id: strategy},
        [{'symbol': '300001', 'strategy_id': 'first_move_5d'}],
        now.date(), now,
    )
    assert len(events) == 1
    assert events[0].due_time == time(14, 30)
    assert events[0].payload['strategy_id'] == 'first_move_5d'
