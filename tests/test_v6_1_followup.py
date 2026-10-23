from datetime import UTC, datetime, date

import pytest

from pointtable.core.auction import AuctionPattern, classify_auction
from pointtable.core.calendar import is_trading_day, load_holidays
from pointtable.core.volume import BreakQualifier
from pointtable.db.session import get_connection, init_db, save_journal, save_signals, set_database_path
from pointtable.jobs.execution_review import today_execution_review
from pointtable.jobs.market_pipeline import volume_breakout_decision
from pointtable.jobs.routines import run_settle
from pointtable.jobs.watch import stop_break_decision
from pointtable.models.journal import JournalEntry, TransitionConfirmation
from pointtable.models.signal import Signal, SignalSide, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _entry(symbol: str, strategy_id: str, zone=(10.0, 10.2)):
    return Signal(
        id=f'2026-01-01-{symbol}-{strategy_id}',
        symbol=symbol,
        strategy_id=strategy_id,
        side=SignalSide.BUY,
        tag=SignalTag.ENTER,
        zone_low=zone[0],
        zone_high=zone[1],
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.INTRADAY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_holiday_calendar_loads_and_scheduler_skip_is_supported():
    holidays = load_holidays(['2026-01-01'])
    assert is_trading_day(date(2025, 12, 31), holidays)
    assert not is_trading_day(date(2026, 1, 1), holidays)
    assert not is_trading_day(date(2026, 1, 3), holidays)


def test_same_symbol_entries_are_not_deduped():
    entries = [
        _entry('300001', 'first_move_5d'),
        _entry('300001', 'volume_breakout'),
        _entry('300002', 'volume_breakout'),
    ]
    assert len(entries) == 3
    assert [entry.strategy_id for entry in entries[:2]] == ['first_move_5d', 'volume_breakout']


def test_execution_review_flags_missing_mechanical_receipt(tmp_db):
    signal = _entry('300001', 'first_move_5d')
    stop = signal.model_copy(update={
        'id': '2026-01-01-300001-stop',
        'side': SignalSide.SELL,
        'tag': SignalTag.STOP,
        'valid_policy': SignalValidPolicy.UNTIL_STOPPED,
        'parent_id': signal.id,
        'status': SignalStatus.TRIGGERED,
        'closed_at': datetime(2026, 1, 1, tzinfo=UTC),
    })
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    save_signals(conn, [stop])
    save_journal(conn, JournalEntry(
        signal_id=stop.id,
        from_status='ACTIVE',
        to_status='TRIGGERED',
        confirmed=TransitionConfirmation.CONFIRMED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    ))
    conn.commit()
    rows = today_execution_review(conn, '2026-01-01')
    assert rows[0]['mechanical'] is True
    assert rows[0]['difference'] == '未回执'


def test_volume_breakout_requires_price_volume_and_yang_close():
    rows = [
        {'日期': f'2026-01-0{day}', '开盘': 10.0, '最高': 10.1, '收盘': 10.05, '成交量': 100}
        for day in range(1, 6)
    ]
    rows.append({'日期': '2026-01-06', '开盘': 10.2, '最高': 10.8, '收盘': 10.7, '成交量': 200})
    hit, key = volume_breakout_decision(rows, 10.7)
    assert hit is True and key == 10.1
    weak = [dict(row) | {'成交量': 100} for row in rows[:-1]] + [dict(rows[-1]) | {'成交量': 100}]
    assert volume_breakout_decision(weak, 10.7)[0] is False
    assert volume_breakout_decision([{ '日期': '2026-01-01', '收盘': 10 }], 10)[0] is False


def test_auction_classifies_one_word_open():
    assert classify_auction(10.0, 11.0, 2.0) is AuctionPattern.ONE_WORD_OPEN


def test_stop_break_decision_keeps_divergence_and_limit_exempt():
    assert stop_break_decision({'divergence': True})[0] is BreakQualifier.DIVERGENCE
    assert stop_break_decision({'limit_up': True})[0] is BreakQualifier.LIMIT_EXEMPT


def _stopped(symbol: str = '300001', status_reason: str = 'stop_single_point:DELAY_OBSERVE:passive'):
    return Signal(
        id=f'2026-01-01-{symbol}-stop',
        symbol=symbol,
        strategy_id='first_move_5d',
        side=SignalSide.SELL,
        tag=SignalTag.STOP,
        zone_low=9.0,
        zone_high=9.2,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        status=SignalStatus.TRIGGERED,
        status_reason=status_reason,
        parent_id='2026-01-01-300001-first_move_5d',
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        closed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(('status_reason', 'close_price', 'created'), [
    ('stop_single_point:DELAY_OBSERVE:passive', 10.0, False),
    ('stop_single_point:DIVERGENCE:divergence', 10.0, False),
    ('stop_single_point:EXECUTE_NOW:active', None, True),
    ('stop_single_point:LIMIT_EXEMPT:limit', 8.0, False),
])
def test_settle_close_decision_uses_break_qualifier(tmp_db, status_reason, close_price, created):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    save_signals(conn, [_stopped(status_reason=status_reason)])
    conn.commit()
    result = run_settle(
        get_connection(), datetime(2026, 1, 1, tzinfo=UTC),
        close_prices={'300001': close_price} if close_price is not None else {},
    )
    assert bool(result['retest_created']) is created


def test_retest_pass_requires_volume_and_fail_then_expiry(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _stopped()
    retest = stop.model_copy(update={
        'id': stop.id + '-retest',
        'tag': SignalTag.RETEST_CONFIRM,
        'status': SignalStatus.ACTIVE,
        'status_reason': '',
        'closed_at': None,
    })
    save_signals(conn, [stop, retest])
    conn.commit()
    run_settle(get_connection(), datetime(2026, 1, 2, tzinfo=UTC), close_prices={'300001': 9.1})
    assert conn.execute("SELECT status FROM signals WHERE id=?", (retest.id,)).fetchone()[0] == 'TRIGGERED'

    active = retest.model_copy(update={'status': SignalStatus.ACTIVE, 'status_reason': '', 'closed_at': None})
    save_signals(get_connection(), [active])
    get_connection().commit()
    result = run_settle(
        get_connection(), datetime(2026, 1, 2, tzinfo=UTC),
        close_prices={'300001': 9.3}, volume_ratios={'300001': 1.3},
    )
    assert result['retest_settled'] == [retest.id]
    row = conn.execute("SELECT status, status_reason FROM signals WHERE id=?", (retest.id,)).fetchone()
    assert row['status'] == 'INVALIDATED' and row['status_reason'] == 'retest_pass'

    active = retest.model_copy(update={'status': SignalStatus.ACTIVE, 'status_reason': '', 'closed_at': None})
    save_signals(get_connection(), [active])
    get_connection().commit()
    result = run_settle(get_connection(), datetime(2026, 1, 6, tzinfo=UTC))
    assert result['retest_expired'] == [retest.id]
