import json
import pytest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pointtable.data.mock import MockDataSource
from pointtable.data import akshare
from pointtable.cli import main
from pointtable.db.session import get_connection, init_db, save_journal, save_signals, set_database_path
from pointtable.models.journal import JournalEntry
from pointtable.jobs.evening import build_context_marks, build_evening_signals, market_tier
from pointtable.jobs.auction_call import run_auction
from pointtable.jobs.evening import build_market_input, lhb_decision, run_evening
from pointtable.jobs.evening import _volume_ratio, latest_entry_parent_ids, truncate_watchlist
from pointtable.jobs.restore_check import capture_tail_snapshots, load_tail_prices, run_restore
from pointtable.cli import Routine, next_routine
from pointtable.core.generator import build_pool_plan, build_pool_plans
from pointtable.jobs.settle import run_settle
from pointtable.jobs.watch import run_watch
from pointtable.core.generator import (
    TSizeChoice,
    apply_size_hints,
    apply_t_size_choice,
    generate_entry,
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
    with (
        patch.object(akshare, '_tencent_kline', return_value=[]),
        patch.object(akshare, 'cached_ak', return_value=source['stock']),
    ):
        assert akshare.stock_kline('300001', '2026-01-01') == [{'trade_date': '2026-01-01', 'close': 1}]
        assert akshare.stock_kline('300001', limit=1) == [{'trade_date': '2026-01-02', 'close': 2}]
        assert akshare.margin_rows('2026-01-01') == [{'trade_date': '2026-01-01', 'close': 1}]


def test_tencent_kline_handles_quote_without_kline_rows():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'data': {'bj920045': {'qt': {'bj920045': ['0'] * 38}}}}

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.object(akshare.requests, 'Session', return_value=FakeSession()):
        assert akshare._tencent_kline('920045', '2026-10-01', '2026-10-23', 60) == []


def test_tencent_kline_does_not_mix_live_quote_into_history():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            quote = ['0'] * 45
            quote[30] = '20261026095018'
            quote[37] = '1000'
            return {
                'data': {
                    'sh000001': {
                        'day': [['2026-10-23', '1', '2', '3', '0.5', '10']],
                        'qt': {'sh000001': quote},
                    }
                }
            }

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.object(akshare.requests, 'Session', return_value=FakeSession()):
        rows = akshare._tencent_kline('sh000001', '2026-10-01', '2026-10-23', 60)
        assert rows == [{
            'date': '2026-10-23', 'open': 1.0, 'close': 2.0,
            'high': 3.0, 'low': 0.5, 'volume': 10.0,
        }]


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

    with (
        patch.object(akshare, '_tencent_kline', return_value=[]),
        patch.object(akshare, 'cached_ak', cached_ak),
    ):
        today = __import__('datetime').date.today().isoformat()
        rows = akshare.index_kline('sh000001', today, limit=2)
    assert rows[-1]['date'] == __import__('datetime').date.today()
    assert rows[-1]['close'] == 3658.0256


def test_generator_context_time_trigger_and_size_hints():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    marks = build_context_marks(
        [{'symbol': 'sh000001', 'zone': (3000, 3010)}],
        [{'symbol': 'new_energy', 'zone': (100, 110)}],
        now.date(), now,
    )
    assert [mark.context_type.value for mark in marks] == ['index', 'sector']
    rows = apply_size_hints([
        _signal(size_hint=10),
        _signal(side='SELL', tag=SignalTag.STOP, size_hint=10, parent_id='p'),
    ], 'low')
    assert rows[0].size_hint == 5
    assert rows[1].size_hint == 10
    assert rows[0].size_hint == 5


def test_generate_entry_links_reentry_parent():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategy = Strategy(
        id='first_move_10d', side='BUY', premise='再入场',
        y={'type': 'ma_tomorrow', 'n': 10},
        z={'type': 'ma_break', 'n': 10},
        w={'type': 'pressure_zone'},
    )
    rows = generate_entry(
        strategy, '300001', (10, 10.2), now,
        parent_id='old-enter',
    )
    assert rows[0].parent_id == 'old-enter'
    assert len(rows) == 1


def test_watch_process_touch_and_premise():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated, journal, reason = process_touch(_signal(), 10.5, now=now)
    assert updated.status is SignalStatus.TRIGGERED
    assert journal is not None and journal.record_type.value == 'transition'
    assert reason == 'entry_single_point'
    invalid, _ = invalidate_premise(_signal(), now)
    assert invalid.status is SignalStatus.INVALIDATED
    assert qualify_break(volume_ratio=0.5, active_delta=0, stock_triggered=True, sector_triggered=False, index_triggered=False).value == 'EXECUTE_NOW'
    assert qualify_break(volume_ratio=0.5, active_delta=0, stock_triggered=True, sector_triggered=True, index_triggered=False).value == 'DELAY_OBSERVE'


def test_settle_retest_pass_and_fail():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='p')
    retest = stop.model_copy(update={'id': 'r', 'tag': SignalTag.RETEST_CONFIRM, 'side': 'SELL'})
    passed, _ = settle_retest(retest, True, 10.5, now)
    failed, _ = settle_retest(retest, False, 10.5, now)
    assert passed.status is SignalStatus.INVALIDATED
    assert failed.status is SignalStatus.TRIGGERED


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
    # §4：entry 生成时 Z/W 同生 stop/profit 一起落库
    assert [row.tag for row in rows] == [SignalTag.ENTER, SignalTag.STOP, SignalTag.PROFIT]
    assert build_context_marks([{'symbol': 'sh000001', 'zone': (3000, 3010)}], [], now.date(), now)[0].code == 'sh000001'
    assert market_tier(8000) == 'LOW'


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
        price=10.5, created_at=now,
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

    result = run_watch(
        conn, 10.5, False, now, trade_date='2026-01-01',
        volume_ratio=0.7,
        break_context={'300001': {'volume_ratio': 0.7, 'active_delta': 0}},
    )
    assert result['touched'] == ['stop-1']
    assert dict(conn.execute('SELECT status FROM signals WHERE id=?', ('stop-1',)).fetchone())['status'] == 'TRIGGERED'

    result = run_settle(conn, now, close_prices={'300001': 9.9})
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


def test_settle_catches_up_close_touch_without_watch(tmp_db):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        SignalTag.STOP, side='SELL', parent_id='pool:300001',
    ).model_copy(update={
        'id': 'stop-1',
        'valid_policy': SignalValidPolicy.UNTIL_STOPPED,
        'created_at': now,
    })
    save_signals(conn, [stop])
    run_settle(
        conn,
        now,
        close_prices={'300001': 9.9},
        low_prices={'300001': 9.9},
        high_prices={'300001': 10.4},
    )
    row = conn.execute(
        'SELECT status, status_reason, closed_at FROM signals WHERE id=?',
        ('stop-1',),
    ).fetchone()
    assert row['status'] == 'TRIGGERED'
    assert row['status_reason'] == 'settle_close_touch:UNCLASSIFIED:NOT_CLASSIFIED'


def test_settle_resolves_retest_by_close(tmp_db):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    failed = _signal(SignalTag.RETEST_CONFIRM, side='SELL', parent_id='p').model_copy(
        update={'id': 'retest-fail', 'strategy_id': 'holding'}
    )
    passed = failed.model_copy(update={'id': 'retest-pass', 'symbol': '300002'})
    save_signals(conn, [passed, failed])
    result = run_settle(
        conn, now,
        close_prices={'300001': 10.5, '300002': 11.2},
        volume_ratios={'300002': 1.5},
    )
    assert result['retest_settled'] == ['retest-fail', 'retest-pass']
    assert conn.execute('SELECT status FROM signals WHERE id=?', ('retest-pass',)).fetchone()[0] == 'INVALIDATED'
    assert conn.execute('SELECT status FROM signals WHERE id=?', ('retest-fail',)).fetchone()[0] == 'TRIGGERED'


def test_evening_generates_pool_signals(tmp_db):
    now = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    set_database_path(tmp_db)
    init_db()
    payload = {
        'trade_date': '2026-01-01',
        'created_at': now.isoformat(),
        'plans': [],
        'signal_rows': [{
            'symbol': '300001',
            'parent_id': 'parent',
            'trend': 'UP',
            'stop_zone': (9.0, 9.2),
            'profit_zone': (11.0, 11.2),
            't_buy_zone': (10.0, 10.2),
            't_sell_zone': (11.0, 11.2),
        }],
    }
    input_path = Path.cwd() / 'test_evening_pool_signals.json'
    input_path.write_text(json.dumps(payload), encoding='utf-8')
    try:
        result = run_evening(get_connection(), input_path)
        assert len(result['signal_ids']) == 4
        rows = [row['tag'] for row in get_connection().execute(
            'SELECT tag FROM signals ORDER BY id'
        ).fetchall()]
        assert set(rows) == {'stop', 'profit', 't'}
        assert rows.count('t') == 2
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
        # §4：entry + Z/W 同生 stop/profit 共 3 行
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
    assert plan['entry_low'] >= plan['stop_high']
    assert plan['invalidation_price'] == plan['stop_high']
    assert plan['entry_low'] < plan['entry_high'] < plan['profit_low']
    plans = build_pool_plans([strategy], [{
        'symbol': '300001', 'strategy_id': 'first_move_5d',
        'closes': [10.0, 10.0, 10.0, 10.0], 'est_close': 10.2,
        'premise_facts': {'hot': True},
    }])
    assert plans == [plan]


def test_pool_plan_rejects_invalidation_inside_entry():
    strategy = Strategy(
        id='pullback', side='BUY', premise='support',
        y={'type': 'pullback'}, z={'type': 'close_below'},
        w={'type': 'next_resistance'},
    )
    plan = build_pool_plan(
        strategy, '300001', [10, 10, 10, 10], 10.2,
        support=10.0, resistance=11.0, breakout_price=10.1,
    )
    assert plan == {}


def test_stock_spot_quotes_uses_pool_symbols_only(monkeypatch):
    seen = []

    def fake_parts(symbols):
        seen.extend(symbols)
        return [['51', '测试股', '300001', '10.2', '10.0']]

    monkeypatch.setattr(akshare, '_tencent_quote_parts', fake_parts)
    monkeypatch.setattr(
        akshare, 'stock_spot_rows',
        lambda: pytest.fail('pool quotes must not fall back to full market'),
    )
    rows = akshare.stock_spot_quotes(['300001'])
    assert seen == ['300001']
    assert rows == [{'symbol': '300001', 'name': '测试股', 'price': 10.2}]


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
        'amount_yi': 8000, 'tier': 'LOW',
        'sector_down': True, 'pool_reversal': True, 't_symbols': [],
    }, [{'name': 'A', 'progress_pct': 1, 'premise': '主力净流出', 'stop_n': 10, 'stop_low': 9, 'stop_high': 9.2}])
    assert '【持仓】' in text and '止损放宽到10日线9–9.2' in text
    assert '资金总要找个宣泄口' in text and '8000亿' in text


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
    signal = _signal().model_copy(update={
        'id': '2026-01-01-002975-first_move_5d',
        'symbol': '002975',
        'strategy_id': 'first_move_5d',
    })
    conn.commit()
    save_signals(conn, [signal])
    conn.commit()
    assert main(['--db', str(tmp_db), 'report', '--date', '2026-01-01']) == 0
    output_path = Path.cwd() / 'test_report.md'
    try:
        assert main(['--db', str(tmp_db), 'report', '--date', '2026-01-01', '--output', str(output_path)]) == 0
        text = output_path.read_text(encoding='utf-8')
        assert '【2026-01-01 交易计划】' in text
        assert '002975' in text and 'entry  5日线首动  10–11' in text
    finally:
        output_path.unlink(missing_ok=True)


def test_market_pipeline_builds_assignments_and_context():
    now = datetime(2026, 1, 1, tzinfo=UTC).date()
    index_rows = [
        {'日期': '2026-01-01', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-01', '收盘': 10 + index * 0.1, '最高': 10.2 + index * 0.1, '最低': 9.8}
        for index in range(20)
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
        regulator_list=[],
        now=now,
    )
    assert payload['market_tier'] == 'HIGH'
    assert payload['assignments'][0]['strategy_id'] == 'first_move_5d'
    assert payload['assignments'][0]['premise_facts']['not_regulated'] is True
    assert payload['rejected'] == []
    assert payload['sector_context'] == [{'symbol': 'AI', 'zone': (990.0, 1010.0)}]
    assert payload['stop_zones']['300001'][0] < payload['stop_zones']['300001'][1]


def test_volume_ratio_uses_prior_five_day_average():
    rows = [
        {'date': f'2026-01-0{day}', 'volume': 100.0}
        for day in range(1, 6)
    ]
    rows.append({'date': '2026-01-06', 'volume': 150.0})
    assert _volume_ratio(rows, '2026-01-06') == 1.5
    assert _volume_ratio(rows, '2026-01-05') is None


def test_market_pipeline_blocks_new_buy_when_lhb_unchecked():
    index_rows = [
        {'日期': '2026-01-01', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-01', '收盘': 10 + index * 0.1, '最高': 10.2 + index * 0.1, '最低': 9.8}
        for index in range(20)
    ]
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        index_rows,
        [],
        {'300001': stock_rows},
        {'AI': stock_rows},
        now=datetime(2026, 1, 1, tzinfo=UTC).date(),
        lhb_checked=False,
    )
    assert payload['assignments'] == []
    assert payload['rejected'] == [{'symbol': '300001', 'reason': 'lhb_data_unavailable'}]


def test_sector_kline_filters_current_history_window(monkeypatch):
    from pointtable.data import akshare

    calls = []

    def fake_cached_ak(name, **kwargs):
        calls.append((name, kwargs))
        if 'indu' not in name:
            return []
        return [
            {'日期': '2026-01-01', '收盘': 100},
            {'日期': '2026-01-02', '收盘': 101},
            {'日期': '2026-01-03', '收盘': 102},
        ]

    monkeypatch.setattr(akshare, 'cached_ak', fake_cached_ak)
    rows = akshare.sector_kline('半导体', trade_date='2026-01-02', limit=2)
    assert [row['日期'] for row in rows] == ['2026-01-01', '2026-01-02']
    assert calls[0][1]['end_date'] == '20260102'


def test_sina_lhb_fallback_does_not_filter_undated_rows(monkeypatch):
    from pointtable.data import akshare

    monkeypatch.setattr(
        akshare,
        'cached_ak',
        lambda name, **kwargs: None if name == 'stock_lhb_detail_em' else [
            {'股票代码': '300001'},
            {'股票代码': '300002'},
        ],
    )
    rows, checked = akshare.lhb_rows_checked('2026-01-02')
    assert checked is True
    assert [row['股票代码'] for row in rows] == ['300001', '300002']


def test_market_pipeline_fills_missing_spot_quotes(monkeypatch):
    from pointtable.jobs.evening import fill_missing_spot_rows

    rows = fill_missing_spot_rows(
        [],
        ['300001'],
        lambda symbols: [{'symbol': '300001', 'name': '', 'price': 12.3}],
    )
    assert rows == [{'symbol': '300001', 'name': '', 'price': 12.3}]


def test_selector_uses_facts_and_family_cooldown():
    index_rows = [
        {'日期': '2026-01-01', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-01', '收盘': 10 + index * 0.1, '最高': 20.0, '最低': 9.8}
        for index in range(20)
    ]
    payload = build_market_input(
            [{'symbol': '300001', 'theme_label': 'AI'}, {'symbol': '300002', 'theme_label': 'AI'}],
        index_rows,
        [],
        {'300001': stock_rows, '300002': stock_rows},
        {},
        active_retest={'300002'},
        recent_stops={'300001': {'ma5': 2}},
            now=datetime(2026, 1, 1, tzinfo=UTC).date(),
    )
    strategies = [row['strategy_id'] for row in payload['assignments']]
    assert 'first_move_10d' in strategies
    assert 'first_move_5d' not in strategies
    assert payload['rejected'] == [{'symbol': '300002', 'reason': 'active_retest_confirm'}]


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
        source_rows=[{'symbol': '300001', 'price': 12.0}],
    )
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        [
            {'日期': '2026-01-01', '收盘': 4000, 'amount': 13_000_000_000_000}
        for _ in range(20)
        ],
        [],
        {'300001': [{'日期': '2026-01-01', '收盘': 10 + index * 0.1} for index in range(20)]},
        {},
        tail_prices=load_tail_prices(conn, '2026-01-01'),
        now=now.date(),
    )
    assert payload['assignments'][0]['est_close'] == 12.0


def test_tail_snapshot_appends_missing_close_for_tomorrow_line(tmp_db):
    now = datetime(2026, 1, 2, tzinfo=UTC).date()
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        [
            {'日期': '2026-01-01', '收盘': 4000, 'amount': 13_000_000_000_000}
            for _ in range(19)
        ] + [{'日期': '2026-01-02', '收盘': 4000, 'amount': 13_000_000_000_000}],
        [],
        {'300001': [{'日期': '2026-01-01', '收盘': close} for close in (9, 10, 11, 12, 13, 14)]},
        {},
        tail_prices={'300001': 15.0},
        now=now,
    )
    assignment = payload['assignments'][0]
    assert assignment['closes'][-1] == 15.0
    assert assignment['est_close'] == 15.0
    assert payload['stop_zones']['300001'][1] == 13.0


def test_latest_entry_parent_prefers_triggered_enter(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    base = _signal().model_copy(update={'id': 'enter-0'})
    reentry = base.model_copy(update={
        'id': 'enter-1',
        'parent_id': 'enter-0',
        'strategy_id': 'first_move_10d',
        'status': SignalStatus.TRIGGERED,
    })
    save_signals(conn, [base, reentry])
    conn.commit()
    assert latest_entry_parent_ids(conn, ['300001']) == {'300001': 'enter-1'}


def test_latest_entry_parent_falls_back_to_pool(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    base = _signal().model_copy(update={'id': 'enter-0'})
    save_signals(conn, [base])
    assert latest_entry_parent_ids(conn, ['300001']) == {'300001': 'pool:300001'}
    stop = _signal(SignalTag.STOP, side='SELL', parent_id='enter-0').model_copy(update={
        'id': 'stop-0',
        'valid_policy': SignalValidPolicy.UNTIL_STOPPED,
        'status': SignalStatus.TRIGGERED,
        'created_at': datetime(2026, 1, 2, tzinfo=UTC),
    })
    save_signals(conn, [stop])
    assert latest_entry_parent_ids(conn, ['300001']) == {'300001': 'pool:300001'}


def test_lhb_decision_matches_code_and_dumpers():
    rows = [{'代码': '300001', '卖出占比': '40', '营业部': '某砸盘席位'}]
    assert lhb_decision(rows, '300001', ['砸盘席位'])[0] is True
    assert lhb_decision([], '300001', ['砸盘席位']) == (False, 'not_on_lhb')
