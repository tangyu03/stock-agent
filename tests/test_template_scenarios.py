from datetime import UTC, datetime
from pathlib import Path

from pointtable.db.session import get_connection, init_db, save_journal, save_signals, set_database_path
from pointtable.jobs.market_pipeline import build_market_input, recent_stop_counts
from pointtable.models.signal import (
    Signal,
    SignalSide,
    SignalStatus,
    SignalTag,
    SignalTrend,
    SignalValidPolicy,
)
from pointtable.notify.render import render_signal_report
from pointtable.models.journal import JournalEntry


def _signal(
    signal_id: str,
    symbol: str,
    tag: SignalTag,
    side: SignalSide,
    *,
    strategy_id: str = 'first_move_5d',
    status: SignalStatus = SignalStatus.ACTIVE,
    break_qualifier: str | None = None,
    status_reason: str = '',
    created_at: datetime | None = None,
    parent_id: str | None = None,
    valid_policy: SignalValidPolicy = SignalValidPolicy.INTRADAY,
) -> Signal:
    created_at = created_at or datetime(2026, 1, 1, tzinfo=UTC)
    parent_id = parent_id or (f'pool:{symbol}' if tag is not SignalTag.ENTER else None)
    return Signal(
        id=signal_id,
        symbol=symbol,
        strategy_id=strategy_id,
        side=side,
        tag=tag,
        zone_low=68.0,
        zone_high=68.5,
        trend=SignalTrend.UP,
        valid_policy=valid_policy,
        status=status,
        status_reason=status_reason,
        break_qualifier=break_qualifier,
        parent_id=parent_id,
        created_at=created_at,
        closed_at=created_at if status is not SignalStatus.ACTIVE else None,
    )


def test_report_covers_no_break_break_retest_and_judgments():
    report_date = '2026-01-03'
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    signals = [
        _signal('active-stop', '300001', SignalTag.STOP, SignalSide.SELL),
        _signal(
            'broken-stop', '300002', SignalTag.STOP, SignalSide.SELL,
            status=SignalStatus.TRIGGERED,
            break_qualifier='DELAY_OBSERVE',
            status_reason='stop_single_point:DELAY_OBSERVE:passive_or_low_volume_break',
        ),
        _signal(
            'observation', '300003', SignalTag.RETEST_CONFIRM, SignalSide.NEUTRAL,
            parent_id='pool:300003', valid_policy=SignalValidPolicy.FIXED_WINDOW,
        ),
        _signal(
            'judgment-fail', '300005', SignalTag.RETEST_CONFIRM, SignalSide.NEUTRAL,
            status=SignalStatus.TRIGGERED,
            break_qualifier='DELAY_OBSERVE',
            status_reason='retest_fail',
            parent_id='pool:300005',
            valid_policy=SignalValidPolicy.FIXED_WINDOW,
        ),
        _signal(
            'judgment-pass', '300006', SignalTag.RETEST_CONFIRM, SignalSide.NEUTRAL,
            status=SignalStatus.INVALIDATED,
            status_reason='retest_pass',
            parent_id='pool:300006',
            valid_policy=SignalValidPolicy.FIXED_WINDOW,
        ),
        _signal(
            'judgment-expired', '300007', SignalTag.RETEST_CONFIRM, SignalSide.NEUTRAL,
            status=SignalStatus.EXPIRED,
            status_reason='retest_window_expired',
            parent_id='pool:300007',
            valid_policy=SignalValidPolicy.FIXED_WINDOW,
        ),
        _signal(
            'cooled-candidate', '300008', SignalTag.ENTER, SignalSide.BUY,
            strategy_id='first_move_10d',
            valid_policy=SignalValidPolicy.NEXT_DAY,
        ),
    ]
    market = {
        'signal_date': '2026-01-01',
        'name_map': {
            '300001': '不破股', '300002': '破位股', '300003': '观察股',
            '300005': '失败股', '300006': '假破股', '300007': '到期股',
            '300008': '再买股',
        },
        'tier': 'LOW',
        'assignments': [
            {'symbol': '300003', 'est_close': 67.9, 'high': 68.3},
            {'symbol': '300005', 'est_close': 67.9, 'high': 68.3},
            {'symbol': '300006', 'est_close': 68.9, 'high': 68.9, 'volume_ratio': 1.4},
        ],
        'recent_stop_counts': {'300008': {'ma5': 2}},
        'retest_window_days': 3,
    }
    text = render_signal_report(report_date, market, signals)
    assert '不破股（300001）【止损】' in text
    assert '破位股（300002）【止损】' in text and '延迟观察' in text
    assert '观察股（300003）【回踩确认】' in text
    assert '今日最高68.3摸到区间，收67.9未站上' in text
    assert '剩2日。（观察期内不出买点）' in text
    assert '失败股（300005）【回踩确认】' in text
    assert '3日反抽未收复68.5（最高68.3），原支撑确认转为压力' in text
    assert '假破股（300006）【回踩确认】' in text
    assert '收盘68.9收复观察区间上沿68.5，量比1.4（达标）' in text
    assert '原止损逻辑失效' in text
    assert '到期股（300007）【回踩确认】' in text
    assert '3日窗口到期' in text
    assert '再买股（300008） 10日线首动' in text
    assert '5日线档位冷却中' in text


def test_recent_stop_counts_feed_same_ma_cooldown(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    entry = _signal(
        'entry', '300001', SignalTag.ENTER, SignalSide.BUY,
        strategy_id='first_move_5d', valid_policy=SignalValidPolicy.NEXT_DAY,
    )
    stop_one = _signal(
        'stop-one', '300001', SignalTag.STOP, SignalSide.SELL,
        status=SignalStatus.TRIGGERED, parent_id=entry.id,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    stop_two = _signal(
        'stop-two', '300001', SignalTag.STOP, SignalSide.SELL,
        status=SignalStatus.TRIGGERED, parent_id=entry.id,
        created_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    save_signals(conn, [entry, stop_one, stop_two])
    for signal in (stop_one, stop_two):
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status='ACTIVE',
            to_status='TRIGGERED',
            reason='stop_single_point',
            created_at=signal.created_at,
        ))
    conn.commit()
    counts = recent_stop_counts(conn, '2026-01-06', 2, set())
    assert counts == {'300001': {'ma5': 2}}
    index_rows = [
        {'日期': '2026-01-06', '收盘': 4000, '最高': 4010, '最低': 3990, 'amount': 13_000_000_000_000}
        for _ in range(20)
    ]
    stock_rows = [
        {'日期': '2026-01-06', '收盘': 10 + index * 0.1, '最高': 20.0, '最低': 9.8}
        for index in range(20)
    ]
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        index_rows,
        [],
        {'300001': stock_rows},
        {},
        recent_stops=counts,
        now=datetime(2026, 1, 6, tzinfo=UTC).date(),
    )
    strategies = [row['strategy_id'] for row in payload['assignments']]
    assert 'first_move_5d' not in strategies
    assert 'first_move_10d' in strategies
    assert payload['recent_stop_counts'] == counts
