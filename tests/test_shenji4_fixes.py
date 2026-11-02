"""shenji4 审计修正的验收锚点（fangan.md §10）。

覆盖四组修正：
1. 量能=沪+深合计，档位只按合计计算（固定样例 2026-10-29）；
2. 候选对账闭合：selector/生成阶段每次丢弃都有结构化处置记录；
3. 未决 ANOMALOUS：抑制新 entry、维持池内保护；
4. 渲染：pool 伪家族不再产生冷却标签、A0 parent=pool 措辞、event_source 投影。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from pointtable.core.generator import Strategy, build_pool_plans
from pointtable.db.session import (
    get_connection,
    init_db,
    load_signals,
    save_signals,
    save_stop_settlement,
    set_database_path,
)
from pointtable.jobs.evening import (
    anomalous_settlement_symbols,
    build_evening_signals,
    build_market_input,
    market_tier,
    run_evening,
)
from pointtable.models.signal import (
    Signal,
    SignalStatus,
    SignalTag,
    SignalTrend,
    SignalValidPolicy,
)
from pointtable.notify.render import render_header, render_signal_report


def _strategy() -> Strategy:
    return Strategy(
        id='first_move_5d', side='BUY', premise='热门题材 + 5日线上行',
        y={'type': 'ma_tomorrow', 'n': 5, 'when': 'tail_snapshot'},
        z={'type': 'ma_break', 'n': 5},
        w={'type': 'pressure_zone', 'action': 'sell_part'},
    )


def _plan(symbol: str = '300001') -> dict:
    return {
        'symbol': symbol, 'strategy_id': 'first_move_5d',
        'entry_low': 10.0, 'entry_high': 10.2,
        'stop_low': 9.9, 'stop_high': 10.0,
        'invalidation_price': 9.9, 'invalidation_note': '5日线',
        'rationale': '5日线', 'parent_id': None,
        'premise_facts': {'hot_theme': True, 'not_regulated': True, 'lhb_ok': True},
    }


def _signal(
    signal_id: str,
    symbol: str,
    tag: SignalTag = SignalTag.ENTER,
    *,
    strategy_id: str = 'first_move_5d',
    status: SignalStatus = SignalStatus.ACTIVE,
    parent_id: str | None = None,
    created_at: datetime | None = None,
) -> Signal:
    created_at = created_at or datetime(2026, 1, 1, tzinfo=UTC)
    parent_id = parent_id or (f'pool:{symbol}' if tag is not SignalTag.ENTER else None)
    return Signal(
        id=signal_id,
        symbol=symbol,
        strategy_id=strategy_id,
        side='SELL' if tag is SignalTag.STOP else 'BUY',
        tag=tag,
        zone_low=68.0,
        zone_high=68.5,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        status=status,
        status_reason='',
        parent_id=parent_id,
        created_at=created_at,
    )


def _index_rows(day: str, amount: float, close: float = 3651.43) -> list[dict]:
    return [
        {'日期': day, '收盘': close, '最高': close + 10, '最低': close - 10, 'amount': amount}
        for _ in range(20)
    ]


def _stock_rows(day: str) -> list[dict]:
    return [
        {'日期': day, '收盘': 10 + index * 0.1, '最高': 20.0, '最低': 9.8}
        for index in range(20)
    ]


def test_market_tier_boundaries():
    assert market_tier(8999.99) == 'LOW'
    assert market_tier(9000) == 'NORMAL'
    assert market_tier(12999.99) == 'NORMAL'
    assert market_tier(13000) == 'HIGH'


def test_volume_tier_uses_combined_sh_sz_amount():
    # 固定样例（fangan §10）：2026-10-29 沪 7156.21 亿 + 深 8408.96 亿 = 15565.17 亿 → HIGH
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        _index_rows('2026-10-29', 7156.21e8),
        [],
        {'300001': _stock_rows('2026-10-29')},
        {},
        sz_index_rows=_index_rows('2026-10-29', 8408.96e8, close=11138.2),
        now=date(2026, 10, 29),
    )
    market = payload['market']
    assert market['sh_amount_yi'] == 7156.21
    assert market['sz_amount_yi'] == 8408.96
    assert market['amount_yi'] == 15565.17
    assert market['tier'] == 'HIGH'
    assert market['amount_scope'] == 'sh+sz'


def test_volume_single_index_amount_is_marked_sh_only():
    # 兼容路径：只喂沪市行时不得伪装成两市口径
    payload = build_market_input(
        [{'symbol': '300001', 'theme_label': 'AI'}],
        _index_rows('2026-10-29', 7156.21e8),
        [],
        {'300001': _stock_rows('2026-10-29')},
        {},
        now=date(2026, 10, 29),
    )
    market = payload['market']
    assert market['amount_yi'] == 7156.21
    assert market['sz_amount_yi'] is None
    assert market['amount_scope'] == 'sh_only'
    assert market['tier'] == 'LOW'


def test_missing_sz_amount_is_not_silently_downgraded():
    import pytest

    with pytest.raises(ValueError, match='sz index amount'):
        build_market_input(
            [{'symbol': '300001', 'theme_label': 'AI'}],
            _index_rows('2026-10-29', 7156.21e8),
            [],
            {'300001': _stock_rows('2026-10-29')},
            {},
            sz_index_rows=_index_rows('2026-10-29', 0),
            now=date(2026, 10, 29),
        )


def test_build_evening_signals_records_every_rejection():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    strategies = {'first_move_5d': _strategy()}

    rejections: list[dict[str, str]] = []
    assert build_evening_signals(
        strategies, [_plan()], 'LOW', now,
        recent_stop_counts={'300001': {'ma5': 2}},
        rejections=rejections,
    ) == []
    assert rejections == [{'symbol': '300001', 'reason': 'ma_family_cooldown'}]

    rejections = []
    broken_plan = {**_plan(), 'premise_facts': {'hot_theme': False, 'not_regulated': True, 'lhb_ok': True}}
    assert build_evening_signals(strategies, [broken_plan], 'LOW', now, rejections=rejections) == []
    assert rejections == [{'symbol': '300001', 'reason': 'premise_failed'}]

    rejections = []
    active_entry = _signal('2026-01-01-300001-enter', '300001', strategy_id='first_move_5d')
    assert build_evening_signals(
        strategies, [_plan()], 'LOW', now, existing_signals=[active_entry], rejections=rejections,
    ) == []
    assert rejections == [{'symbol': '300001', 'reason': 'duplicate_active_entry'}]

    rejections = []
    unknown = {**_plan(), 'strategy_id': 'no_such_strategy'}
    assert build_evening_signals(strategies, [unknown], 'LOW', now, rejections=rejections) == []
    assert rejections == [{'symbol': '300001', 'reason': 'strategy_config_missing'}]


def test_build_pool_plans_records_rejections():
    rejections: list[dict[str, str]] = []
    build_pool_plans(
        [_strategy()],
        [{'symbol': '300001', 'strategy_id': 'no_such_strategy', 'closes': [10.0], 'est_close': 10.0}],
        rejections,
    )
    assert rejections == [{'symbol': '300001', 'reason': 'strategy_config_missing'}]


def test_anomalous_settlement_suppresses_entry_but_keeps_protection(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    stop = _signal(
        '2026-01-05-300001-stop', '300001', SignalTag.STOP,
        status=SignalStatus.TRIGGERED,
        created_at=datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
    )
    save_signals(conn, [stop])
    save_stop_settlement(
        conn,
        signal_id=stop.id,
        trade_date='2026-01-06',
        raw_qualifier=None,
        effective_qualifier='PENDING',
        circuit_day=False,
        close_price=67.5,
        zone_low=68.0,
        zone_high=68.5,
        settlement_outcome='PENDING_DATA',
        occurred_at=datetime(2026, 1, 6, 15, 5, tzinfo=UTC),
        data_status='ANOMALOUS',
    )
    conn.commit()
    assert anomalous_settlement_symbols(conn, '2026-01-06') == {'300001'}

    payload = {
        'trade_date': '2026-01-06',
        'created_at': '2026-01-06T15:30:00+08:00',
        'market_tier': 'LOW',
        'strategies_path': str(Path(__file__).parents[1] / 'config' / 'strategies.yaml'),
        'plans': [_plan('300001')],
        'assignments': [],
        'holdings': [],
        'signal_rows': [],
        'recent_stop_counts': {},
        'holidays': [],
    }
    input_path = tmp_db.parent / 'evening-anomalous-test.json'
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    result = run_evening(conn, input_path, '2026-01-07')

    assert {'symbol': '300001', 'reason': 'anomalous_stop_pending'} in result['generation_rejections']
    assert [signal.id for signal in load_signals(conn)] == ['2026-01-05-300001-stop']
    input_path.unlink(missing_ok=True)


def test_pool_pseudo_family_no_longer_labels_cooldown():
    market = {
        'signal_date': '2026-01-01',
        'name_map': {'300008': '再买股'},
        'tier': 'LOW',
        'assignments': [],
        'recent_stop_counts': {'300008': {'pool': 2}},
        'retest_window_days': 3,
    }
    signals = [_signal('c-1', '300008', strategy_id='first_move_10d')]
    text = render_signal_report('2026-01-02', market, signals)
    assert '均线冷却' not in text


def test_real_ma_family_cooldown_label_kept():
    market = {
        'signal_date': '2026-01-01',
        'name_map': {'300008': '再买股'},
        'tier': 'LOW',
        'assignments': [],
        'recent_stop_counts': {'300008': {'ma5': 2}},
        'retest_window_days': 3,
    }
    signals = [_signal('c-1', '300008', strategy_id='first_move_10d')]
    text = render_signal_report('2026-01-02', market, signals)
    assert '均线冷却：5日线档位冷却中' in text


def test_a0_pool_parent_label_and_event_source():
    market = {
        'signal_date': '2026-01-01',
        'name_map': {'002975': '测试股'},
        'tier': 'LOW',
        'assignments': [],
        'retest_window_days': 3,
        'execution_review': [{
            'signal_id': '2026-01-01-002975-first_move_drift-stop',
            'symbol': '002975',
            'strategy_id': 'first_move_drift',
            'tag': 'stop',
            'side': 'SELL',
            'zone_low': 76.35,
            'zone_high': 77.13,
            'parent_id': 'pool:002975',
            'triggered_at': '2026-01-01T15:05:00+08:00',
            'created_at': '2025-12-31T15:30:00+08:00',
            'event_source': 'live_settle',
        }],
    }
    text = render_signal_report('2026-01-02', market, [])
    assert '止损（对应 池内·漂移突破同生）' in text
    assert '收盘兜底结算' in text


def test_header_amount_breakdown():
    header = render_header({
        'amount_yi': 15565.17, 'tier': 'HIGH',
        'sh_amount_yi': 7156.21, 'sz_amount_yi': 8408.96,
    })
    assert '（沪7156.21＋深8408.96）' in header
    assert '。。' not in header
    low_header = render_header({
        'amount_yi': 7156.21, 'tier': 'LOW',
        'sh_amount_yi': 7156.21, 'sz_amount_yi': 0,
    })
    assert '量能 7156.21亿' in low_header
    assert '。。' not in low_header


def test_render_candidate_groups_marks_ev_negative_rows():
    market = {
        'signal_date': '2026-01-01',
        'name_map': {'300008': '再买股'},
        'tier': 'LOW',
        'assignments': [],
        'retest_window_days': 3,
    }
    signal = _signal('c-1', '300008', strategy_id='first_move_10d')
    signal = signal.model_copy(update={'premise': {**signal.premise, 'ev_negative': True}})
    text = render_signal_report('2026-01-02', market, [signal])
    assert 'EV_NEG：仅记录，不预警' in text


def test_pool_section_count_uses_pool_total():
    market = {
        'signal_date': '2026-01-01',
        'name_map': {'300001': '池内股', '300002': '挂起股'},
        'tier': 'LOW',
        'pool_total': 20,
        'assignments': [],
        'retest_window_days': 3,
        'protection_states': {'300002': ['WAIT_REENTRY', 'retest_confirmed']},
    }
    signals = [_signal('p-1', '300001', SignalTag.STOP)]
    text = render_signal_report('2026-01-02', market, signals)
    assert '池内票区（20只）：' in text
