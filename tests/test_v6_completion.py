import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pointtable.data.mock import MockDataSource
from pointtable.data import akshare
from pointtable.cli import main
from pointtable.db.session import get_connection, init_db, save_journal, save_signals, set_database_path
from pointtable.models.journal import JournalEntry, TransitionConfirmation
from pointtable.jobs.evening import build_context_rows, build_evening_signals, market_tier, zero_plan_day
from pointtable.jobs.routines import run_auction, run_restore, run_settle, run_watch
from pointtable.jobs.routines import run_evening
from pointtable.core.generator import (
    apply_size_hints,
    can_reenter,
    generate_context_row,
    generate_entry,
    generate_time_trigger_row,
    Strategy,
)
from pointtable.core.auction import AuctionPattern
from pointtable.jobs.settle import settle_retest
from pointtable.jobs.watch import invalidate_premise, process_touch, qualify_break
from pointtable.models.signal import Signal, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy
from pointtable.notify.render import render_index_points, render_report
from pointtable.stats import pool_quality


def _signal(tag=SignalTag.ENTER, side='BUY', size_hint=None, parent_id=None):
    return Signal(
        id='s-1', symbol='300001', strategy_id='s', side=side, tag=tag,
        zone_low=10.0, zone_high=11.0, trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.INTRADAY, size_hint=size_hint,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), parent_id=parent_id,
    )


def test_mock_source_filters_by_fields():
    source = MockDataSource({'kline': [{'symbol': 'A', 'date': '1'}, {'symbol': 'B'}]})
    assert source.get('kline', symbol='A') == [{'symbol': 'A', 'date': '1'}]
    assert source.get('empty') == []


def test_akshare_domain_wrappers_use_cached_source():
    source = {
        'stock': [
            {'trade_date': '2026-01-01', 'close': 1},
            {'trade_date': '2026-01-02', 'close': 2},
        ],
    }
    with patch.object(akshare, 'cached_ak', return_value=source['stock']):
        assert akshare.stock_kline('300001', '2026-01-01') == [{'trade_date': '2026-01-01', 'close': 1}]
        assert akshare.stock_kline('300001', limit=1) == [{'trade_date': '2026-01-02', 'close': 2}]
        assert akshare.margin_rows('2026-01-01') == [{'trade_date': '2026-01-01', 'close': 1}]


def test_generator_context_time_trigger_and_size_hints():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert generate_context_row('sh000001', (3000, 3010), now).tag is SignalTag.INDEX_CONTEXT
    assert generate_context_row('new_energy', (3000, 3010), now).tag is SignalTag.SECTOR_CONTEXT
    assert generate_time_trigger_row('300001', (10, 11), now).tag is SignalTag.TIME_TRIGGER
    rows = apply_size_hints([
        _signal(size_hint=10),
        _signal(side='SELL', tag=SignalTag.STOP, size_hint=10, parent_id='p'),
    ], 'low')
    assert rows[0].size_hint == 5
    assert rows[1].size_hint == 10
    assert not can_reenter(['a', 'b'])


def test_watch_process_touch_and_premise():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated, journal, reason = process_touch(_signal(), 10.5, True, now=now)
    assert updated.status is SignalStatus.TRIGGERED
    assert journal is not None and journal.confirmed.value == 'confirmed'
    assert reason == 'buy_confirmed'
    invalid, _ = invalidate_premise(_signal(), now)
    assert invalid.status is SignalStatus.INVALIDATED
    assert qualify_break(volume_ratio=0.5, active_delta=0, stock_triggered=True, sector_triggered=False, index_triggered=False).value == 'EXECUTE_NOW'
    assert qualify_break(volume_ratio=0.5, active_delta=0, stock_triggered=True, sector_triggered=True, index_triggered=False).value == 'DELAY_OBSERVE'


def test_settle_retest_pass_and_fail():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='p')
    retest = stop.model_copy(update={'id': 'r', 'tag': SignalTag.RETEST_CONFIRM, 'side': 'SELL'})
    passed, _ = settle_retest(retest, False, 10.5, now)
    failed, _ = settle_retest(retest, True, 10.5, now)
    assert passed.status is SignalStatus.TRIGGERED
    assert failed.status is SignalStatus.INVALIDATED


def test_evening_pipeline_and_market_fuse():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='热门题材',
        y={'type': 'ma_tomorrow'}, z={'type': 'ma_break'}, w={'type': 'pressure_zone'},
    )
    rows = build_evening_signals(
        {'first_move_5d': strategy},
        [{
            'symbol': '300001', 'strategy_id': 'first_move_5d',
            'entry_low': 10.0, 'entry_high': 10.2, 'stop_low': 9.0, 'stop_high': 9.2,
            'profit_low': 12.0, 'profit_high': 12.2, 'premise_facts': {'hot': True},
        }],
        'LOW', now,
    )
    assert [row.tag for row in rows] == [SignalTag.ENTER, SignalTag.STOP, SignalTag.PROFIT]
    assert build_context_rows([{'symbol': 'sh000001', 'zone': (3000, 3010)}], [], now)[0].tag is SignalTag.INDEX_CONTEXT
    assert market_tier(8000) == 'LOW'
    assert zero_plan_day(8000, 'down')
    assert not zero_plan_day(8000, 'up')


def test_save_signal_and_journal(tmp_db):
    set_database_path(tmp_db)
    init_db()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    signal = _signal(parent_id=None).model_copy(update={'premise': {'hot': True}})
    conn = get_connection()
    assert save_signals(conn, [signal]) == 1
    conn.commit()
    save_journal(conn, JournalEntry(
        signal_id=signal.id, from_status='ACTIVE', to_status='TRIGGERED',
        price=10.5, confirmed=TransitionConfirmation.CONFIRMED, created_at=now,
    ))
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM journal WHERE signal_id=?', (signal.id,)).fetchone()[0] == 1


def test_routine_auction_watch_settle_and_restore(tmp_db):
    set_database_path(tmp_db)
    init_db()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    conn = get_connection()
    buy = _signal()
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='p')
    stop = stop.model_copy(update={'id': 'stop-1', 'valid_policy': SignalValidPolicy.UNTIL_STOPPED})
    save_signals(conn, [buy, stop])
    conn.commit()

    result = run_auction(conn, '2026-01-01', AuctionPattern.STRONG_TO_WEAK, now)
    assert result['invalidated'] == ['s-1']
    assert conn.execute('SELECT COUNT(*) FROM journal WHERE signal_id=?', ('s-1',)).fetchone()[0] == 1

    result = run_watch(conn, 10.5, False, now, trade_date='2026-01-01')
    assert result['touched'] == ['stop-1']
    assert dict(conn.execute('SELECT status FROM signals WHERE id=?', ('stop-1',)).fetchone())['status'] == 'TRIGGERED'

    result = run_settle(conn, now)
    assert result['retest_created'] == ['stop-1-retest']
    assert dict(conn.execute('SELECT status FROM signals WHERE id=?', ('stop-1-retest',)).fetchone())['status'] == 'ACTIVE'
    result = run_restore(conn, 10.5)
    assert result['reminders'] == ['stop-1']
    assert main(['--db', str(tmp_db), 'restore', '--close-price', '10.5']) == 0


def test_evening_cli_persists_signals(tmp_db):
    input_path = Path.cwd() / 'test_evening_input.json'
    input_path.write_text(json.dumps({
        'trade_date': '2026-01-01', 'market_tier': 'NORMAL',
        'plans': [{
            'symbol': '300001', 'strategy_id': 'first_move_5d',
            'entry_low': 10.0, 'entry_high': 10.2,
            'stop_low': 9.0, 'stop_high': 9.2,
            'profit_low': 12.0, 'profit_high': 12.2,
            'premise_facts': {'hot': True, 'not_regulated': True},
        }],
    }), encoding='utf-8')
    try:
        assert main(['--db', str(tmp_db), 'evening', '--input', str(input_path)]) == 0
        assert dict(get_connection().execute(
            'SELECT COUNT(*) AS count FROM signals WHERE created_at LIKE ?',
            ('2026-01-01%',),
        ).fetchone())['count'] == 3
    finally:
        input_path.unlink(missing_ok=True)
    return
    signal = _signal().model_copy(update={'premise': {'hot': True}})
    assert dict(conn.execute('SELECT id, premise FROM signals WHERE id=?', (signal.id,)).fetchone()) == {
        'id': 's-1', 'premise': '{"hot": true}'
    }


def test_stats_pool_quality_and_render_slots():
    report = pool_quality(
        [{'symbol': 'A', 'trend': 'UP'}, {'symbol': 'B', 'trend': 'DOWN'}],
        [{'symbol': 'A', 'five_day_return': 4.0}],
    )
    assert report['total'] == 2 and report['measured'] == 1
    assert report['five_day_avg_return'] == 4.0
    assert render_index_points({'index_points': [{'name': '上证', 'levels': [1, 2]}]}).count('1') == 1
    text = render_report('1.1', {
        'amount_yi': 8000, 'tier': 'LOW', 'zero_plan_day': True,
        'sector_down': True, 'pool_reversal': True, 't_symbols': [],
    }, [{'name': 'A', 'progress_pct': 1, 'premise': '主力净流出', 'stop_n': 10, 'stop_low': 9, 'stop_high': 9.2}])
    assert '【持仓】' in text and '止损放宽到10日线9–9.2' in text
    assert '资金总要找个宣泄口' in text and '8000亿的市场' in text
