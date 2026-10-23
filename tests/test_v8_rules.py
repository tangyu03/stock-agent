import json
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
    set_database_path,
)
from pointtable.jobs.evening import build_context_marks
from pointtable.jobs.evening import build_event_calendar
from pointtable.jobs.routines import run_evening, run_settle, run_watch
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


def test_generator_reuses_active_stop():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='hot',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )
    existing = _signal(
        SignalTag.STOP, side='SELL', id='active-stop', strategy_id='holding',
        parent_id='pool:300001', zone_low=9.0, zone_high=9.2,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
    )
    rows = generate_entry(
        strategy, '300001', (10, 10.2), (8.8, 9.0), (12, 12.2), now,
        existing_signals=[existing],
    )
    assert rows[0].trigger_window_start == time(14, 30)
    assert rows[1].id == 'active-stop'
    assert rows[1].parent_id == rows[0].id


def test_active_retest_pauses_holding_rows(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    retest = _signal(
        SignalTag.RETEST_CONFIRM, side='NEUTRAL', id='retest-1',
        strategy_id='holding', parent_id='pool:300001',
        valid_policy=SignalValidPolicy.FIXED_WINDOW,
    )
    save_signals(conn, [retest])
    payload = {
        'trade_date': '2026-01-01',
        'holdings': [{
            'symbol': '300001', 'parent_id': 'pool:300001', 'trend': 'UP',
            'stop_zone': (9, 9.2), 'support_zone': (10, 10.2),
            'resistance_zone': (11, 11.2),
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


def test_melt_day_treats_delay_as_execute(tmp_db):
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
        market_tier='LOW', index_trend='DOWN',
    )
    assert result['retest_created'] == ['stop-1-retest']


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
        'sh_ma5': 3000, 'index_trend': 'UP', 'zero_plan_day': False,
        'name_map': {'300001': '测试'},
    }
    text = build_report('2026-01-01', market)
    for heading in ('【候选】', '【池内票】', '【观察区】'):
        assert heading in text
    assert '现价 10.1' in text


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
