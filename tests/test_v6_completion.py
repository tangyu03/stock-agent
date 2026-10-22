import json
import pytest
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
from pointtable.jobs.market_pipeline import build_market_input, lhb_decision
from pointtable.jobs.market_pipeline import latest_entry_depths, truncate_watchlist
from pointtable.jobs.tail_snapshot import capture_tail_snapshots, load_tail_prices
from pointtable.jobs.scheduler import Routine, next_routine
from pointtable.jobs.plan_builder import build_pool_plan, build_pool_plans
from pointtable.core.generator import (
    TSizeChoice,
    apply_size_hints,
    apply_t_size_choice,
    can_reenter,
    generate_context_row,
    generate_entry,
    generate_time_trigger_row,
    Strategy,
)
from pointtable.core.auction import AuctionPattern
from pointtable.jobs.settle import settle_retest
from pointtable.jobs.watch import invalidate_premise, process_touch, qualify_break
from pointtable.models.signal import (
    Signal, SignalSide, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy,
)
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


def test_index_kline_appends_spot_for_missing_trade_date():
    daily_rows = [{
        'date': __import__('datetime').date(2026, 10, 21),
        'close': 3713.028,
        'amount': 706_095_648_848,
    }]
    spot_rows = [{
        '代码': 'sh000001',
        '最新价': 3658.0256,
        '今开': 3714.476,
        '最高': 3714.476,
        '最低': 3649.8649,
        '成交量': 437_691_963,
        '成交额': 706_095_648_848,
    }]

    def cached_ak(name, **kwargs):
        return spot_rows if name == 'stock_zh_index_spot_sina' else daily_rows

    with patch.object(akshare, 'cached_ak', cached_ak):
        rows = akshare.index_kline('sh000001', '2026-10-22', limit=2)
    assert rows[-1]['date'] == __import__('datetime').date(2026, 10, 22)
    assert rows[-1]['close'] == 3658.0256


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


def test_generate_entry_links_reentry_parent():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_10d', side='BUY', premise='再入场',
        y={'type': 'ma_tomorrow', 'n': 10},
        z={'type': 'ma_break', 'n': 10},
        w={'type': 'pressure_zone'},
    )
    rows = generate_entry(
        strategy, '300001', (10, 10.2), (9, 9.2), (12, 12.2), now,
        parent_id='old-enter',
    )
    assert rows[0].parent_id == 'old-enter'
    assert rows[1].parent_id == rows[0].id


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


def test_watch_invalidates_broken_premise(tmp_db):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    signal = _signal()
    save_signals(conn, [signal])
    result = run_watch(conn, 10.5, True, now, broken_signal_ids=[signal.id])
    assert result['premise_broken'] == [signal.id]
    row = conn.execute('SELECT status, status_reason FROM signals WHERE id=?', (signal.id,)).fetchone()
    assert row['status'] == 'INVALIDATED'
    assert row['status_reason'] == 'premise_broken'


def test_settle_rolls_active_stop_zones(tmp_db):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='parent').model_copy(
        update={'valid_policy': SignalValidPolicy.UNTIL_STOPPED}
    )
    save_signals(conn, [stop])
    result = run_settle(conn, now, {'300001': (10.8, 11.0)})
    assert result['rolled'] == [stop.id]
    row = conn.execute('SELECT zone_low, zone_high FROM signals WHERE id=?', (stop.id,)).fetchone()
    assert row['zone_low'] == 10.8
    assert row['zone_high'] == 11.0


def test_settle_resolves_retest_by_close(tmp_db):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    passed = _signal(SignalTag.RETEST_CONFIRM, side='SELL', parent_id='p').model_copy(
        update={'id': 'retest-pass', 'strategy_id': 'holding'}
    )
    failed = passed.model_copy(update={'id': 'retest-fail', 'symbol': '300002'})
    save_signals(conn, [passed, failed])
    result = run_settle(conn, now, close_prices={'300001': 10.5, '300002': 11.2})
    assert result['retest_settled'] == ['retest-fail', 'retest-pass']
    assert conn.execute('SELECT status FROM signals WHERE id=?', ('retest-pass',)).fetchone()[0] == 'TRIGGERED'
    assert conn.execute('SELECT status FROM signals WHERE id=?', ('retest-fail',)).fetchone()[0] == 'INVALIDATED'


def test_evening_generates_holding_rows(tmp_db):
    now = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    payload = {
        'trade_date': '2026-01-01',
        'created_at': now.isoformat(),
        'plans': [],
        'holdings': [{
            'symbol': '300001',
            'parent_id': 'parent',
            'trend': 'UP',
            'stop_zone': (9.0, 9.2),
            'support_zone': (10.0, 10.2),
            'resistance_zone': (11.0, 11.2),
        }],
    }
    input_path = Path.cwd() / 'test_evening_holdings.json'
    input_path.write_text(json.dumps(payload), encoding='utf-8')
    try:
        result = run_evening(get_connection(), input_path)
        assert len(result['signal_ids']) == 3
        rows = [row['tag'] for row in get_connection().execute(
            'SELECT tag FROM signals ORDER BY id'
        ).fetchall()]
        assert rows == ['stop', 't', 't']
    finally:
        set_database_path(Path('unused'))
        input_path.unlink(missing_ok=True)


def test_evening_cli_persists_signals(tmp_db):
    input_path = Path.cwd() / 'test_evening_input.json'
    input_path.write_text(json.dumps({
        'trade_date': '2026-01-01', 'market_tier': 'NORMAL',
        'assignments': [{
            'symbol': '300001', 'strategy_id': 'first_move_5d',
            'closes': [10.0, 10.0, 10.0, 10.0], 'est_close': 10.2,
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


def test_plan_builder_builds_batch_from_strategy_objects():
    strategy = Strategy(
        id='first_move_5d', side='BUY', premise='热门题材',
        y={'type': 'ma_tomorrow', 'n': 5}, z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone'},
    )
    plan = build_pool_plan(
        strategy, '300001', [10.0, 10.0, 10.0, 10.0], 10.2,
        premise_facts={'hot': True},
    )
    assert plan['strategy_id'] == 'first_move_5d'
    assert plan['entry_low'] < plan['stop_high'] < plan['entry_high'] < plan['profit_low']
    plans = build_pool_plans([strategy], [{
        'symbol': '300001', 'strategy_id': 'first_move_5d',
        'closes': [10.0, 10.0, 10.0, 10.0], 'est_close': 10.2,
        'premise_facts': {'hot': True},
    }])
    assert plans == [plan]


def test_scheduler_skips_weekend_and_orders_routines():
    friday_night = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
    monday = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    after_settle = datetime(2026, 1, 5, 15, 10, tzinfo=UTC)
    assert next_routine(friday_night).routine is Routine.AUCTION
    assert next_routine(monday).run_at.date().isoformat() == '2026-01-05'
    assert next_routine(after_settle).routine is Routine.EVENING
    with pytest.raises(ValueError, match='timezone-aware'):
        next_routine(datetime(2026, 1, 5, 9, 0))
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


def test_t_size_choice_and_cli(tmp_db):
    t_buy = _signal(SignalTag.T, size_hint=10, parent_id='p')
    assert apply_t_size_choice(t_buy, TSizeChoice.DOUBLE).size_hint == 20
    assert apply_t_size_choice(t_buy, TSizeChoice.HALF).size_hint == 5
    assert apply_t_size_choice(t_buy, TSizeChoice.SAME).size_hint == 10
    with pytest.raises(ValueError, match='T BUY'):
        apply_t_size_choice(_signal(SignalTag.STOP, side='SELL', size_hint=10, parent_id='p'), TSizeChoice.HALF)

    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    save_signals(conn, [t_buy])
    conn.commit()
    assert main(['--db', str(tmp_db), 't-size', 's-1', '--choice', 'double']) == 0
    assert dict(get_connection().execute('SELECT size_hint FROM signals WHERE id=?', ('s-1',)).fetchone())['size_hint'] == 20


def test_report_uses_pool_rows(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    conn.execute(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        ('2026-01-01', '002975', 'AI'),
    )
    conn.commit()
    assert main(['--db', str(tmp_db), 'report', '--date', '2026-01-01']) == 0
    output_path = Path.cwd() / 'test_report.md'
    try:
        assert main(['--db', str(tmp_db), 'report', '--date', '2026-01-01', '--output', str(output_path)]) == 0
        text = output_path.read_text(encoding='utf-8')
        assert '【2026-01-01 复盘】' in text
        assert '002975（博杰股份，AI）' in text
    finally:
        output_path.unlink(missing_ok=True)


def test_market_pipeline_builds_assignments_and_context():
    now = datetime(2026, 1, 1, tzinfo=UTC).date()
    index_rows = [
        {'日期': '2026-01-01', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-01', '收盘': 10, '最高': 10.2, '最低': 9.8}
        for _ in range(20)
    ]
    sector_rows = [
        {'日期': '2026-01-01', '收盘': 1000, '最高': 1010, '最低': 990}
        for _ in range(10)
    ]
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        index_rows,
        [],
        {'300001': stock_rows},
        {'AI': sector_rows},
        regulator_list=['300001'],
        now=now,
    )
    assert payload['market_tier'] == 'HIGH'
    assert payload['assignments'][0]['premise_facts']['not_regulated'] is False
    assert payload['rejected'] == []
    assert payload['sector_context'] == [{'symbol': 'AI', 'zone': (990.0, 1010.0)}]
    assert payload['stop_zones']['300001'][0] < payload['stop_zones']['300001'][1]


def test_market_pipeline_selects_reentry_and_enforces_limit():
    index_rows = [
        {'日期': '2026-01-01', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-01', '收盘': 10, '最高': 10.2, '最低': 9.8}
        for _ in range(20)
    ]
    payload = build_market_input(
        [{'symbol': '300001'}, {'symbol': '300002'}],
        index_rows,
        [],
        {'300001': stock_rows, '300002': stock_rows},
        {},
        entry_depths={'300001': 1, '300002': 2},
        entry_parent_ids={'300001': 'enter-0', '300002': 'enter-1'},
            now=datetime(2026, 1, 1, tzinfo=UTC).date(),
    )
    assert payload['assignments'][0]['strategy_id'] == 'first_move_10d'
    assert payload['assignments'][0]['entry_depth'] == 1
    assert payload['assignments'][0]['parent_id'] == 'enter-0'
    assert payload['rejected'] == [{'symbol': '300002', 'reason': 'reentry_limit'}]


def test_truncate_watchlist_uses_priority_then_input_order():
    rows = [
        {'symbol': '300002'},
        {'symbol': '300001'},
        {'symbol': '300003'},
    ]
    assert [row['symbol'] for row in truncate_watchlist(rows, 3)] == ['300002', '300001', '300003']


def test_tail_snapshot_overrides_daily_close(tmp_db):
    now = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    conn.execute(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        ('2026-01-01', '300001', 'AI'),
    )
    capture_tail_snapshots(
        conn, '2026-01-01', now,
        source_rows=[{'symbol': '300001', 'price': 10.5}],
    )
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        [{'日期': '2026-01-01', '收盘': 4000, 'amount': 13_000_000_000_000} for _ in range(20)],
        [],
        {'300001': [{'日期': '2026-01-01', '收盘': 10.2} for _ in range(20)]},
        {},
        tail_prices=load_tail_prices(conn, '2026-01-01'),
        now=now.date(),
    )
    assert payload['assignments'][0]['est_close'] == 10.5


def test_tail_snapshot_appends_missing_close_for_tomorrow_line(tmp_db):
    now = datetime(2026, 1, 2, tzinfo=UTC).date()
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        [{'日期': '2026-01-01', '收盘': 4000, 'amount': 13_000_000_000_000} for _ in range(20)],
        [],
        {'300001': [{'日期': '2026-01-01', '收盘': close} for close in (10, 11, 12, 13)]},
        {},
        tail_prices={'300001': 15.0},
        now=now,
    )
    assignment = payload['assignments'][0]
    assert assignment['closes'][-1] == 15.0
    assert assignment['est_close'] == 15.0
    assert payload['stop_zones']['300001'][1] == 13.2


def test_latest_entry_depths_follows_parent_chain(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    base = _signal().model_copy(update={'id': 'enter-0'})
    reentry = base.model_copy(update={
        'id': 'enter-1',
        'parent_id': 'enter-0',
        'strategy_id': 'first_move_10d',
    })
    save_signals(conn, [base, reentry])
    conn.commit()
    assert latest_entry_depths(conn, ['300001']) == {'300001': 1}


def test_latest_entry_depth_waits_for_triggered_stop(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    base = _signal().model_copy(update={'id': 'enter-0'})
    save_signals(conn, [base])
    assert latest_entry_depths(conn, ['300001']) == {'300001': 0}
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='enter-0').model_copy(update={
        'id': 'stop-0',
        'valid_policy': SignalValidPolicy.UNTIL_STOPPED,
        'status': SignalStatus.TRIGGERED,
        'created_at': datetime(2026, 1, 2, tzinfo=UTC),
    })
    save_signals(conn, [stop])
    assert latest_entry_depths(conn, ['300001']) == {'300001': 1}


def test_lhb_decision_matches_code_and_dumpers():
    rows = [{'代码': '300001', '卖出占比': '40', '营业部': '某砸盘席位'}]
    assert lhb_decision(rows, '300001', ['砸盘席位'])[0] is True
    assert lhb_decision([], '300001', ['砸盘席位']) == (False, 'not_on_lhb')
