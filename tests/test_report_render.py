from datetime import UTC, datetime

from pointtable.models.signal import (
    Signal,
    SignalRole,
    SignalSide,
    SignalStatus,
    SignalTag,
    SignalTrend,
    SignalValidPolicy,
)
from pointtable.notify.render import render_signal_report


def _signal(
    signal_id: str,
    tag: SignalTag,
    side: SignalSide,
    *,
    symbol: str = '300001',
    strategy_id: str = 'first_move_5d',
    status: SignalStatus = SignalStatus.ACTIVE,
    parent_id: str | None = None,
    valid_policy: SignalValidPolicy = SignalValidPolicy.INTRADAY,
    break_qualifier: str | None = None,
    status_reason: str = '',
) -> Signal:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return Signal(
        id=signal_id,
        symbol=symbol,
        strategy_id=strategy_id,
        side=side,
        tag=tag,
        zone_low=10.0,
        zone_high=10.2,
        trend=SignalTrend.UP,
        valid_policy=valid_policy,
        status=status,
        status_reason=status_reason,
        parent_id=parent_id,
        created_at=created_at,
        closed_at=created_at if status is not SignalStatus.ACTIVE else None,
        break_qualifier=break_qualifier,
    )


def test_signal_report_renders_chinese_names_states_and_roles():
    signals = [
        _signal('entry', SignalTag.ENTER, SignalSide.BUY, valid_policy=SignalValidPolicy.NEXT_DAY),
        _signal(
            'pool-stop',
            SignalTag.STOP,
            SignalSide.SELL,
            symbol='300002',
            strategy_id='pool',
            parent_id='entry',
            valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        ),
        _signal(
            'pool-t-buy',
            SignalTag.T,
            SignalSide.BUY,
            symbol='300002',
            strategy_id='pool',
            parent_id='entry',
            valid_policy=SignalValidPolicy.SAME_DAY,
        ),
        _signal(
            'pool-t-sell',
            SignalTag.T,
            SignalSide.SELL,
            symbol='300002',
            strategy_id='pool',
            parent_id='entry',
            valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        ),
        _signal(
            'retest',
            SignalTag.RETEST_CONFIRM,
            SignalSide.NEUTRAL,
                symbol='300003',
            parent_id='pool:300002',
            valid_policy=SignalValidPolicy.FIXED_WINDOW,
        ),
        _signal(
            'judgment',
            SignalTag.RETEST_CONFIRM,
            SignalSide.NEUTRAL,
            parent_id='pool:300001',
            valid_policy=SignalValidPolicy.FIXED_WINDOW,
            status=SignalStatus.TRIGGERED,
            break_qualifier='DELAY_OBSERVE',
            status_reason='retest_fail',
        ),
    ]
    market = {
        'name_map': {'300001': '测试股', '300002': '池内股', '300003': '观察股'},
        'tier': 'LOW',
        'sh_close': 3005,
        'sh_ma5': 3000,
        'execution_review': [{
            'symbol': '300001',
            'strategy_id': 'holding',
            'tag': 'stop',
            'zone_low': 10.0,
            'zone_high': 10.2,
        }],
        'ev_negative_strategies': ['volume_breakout'],
    }
    text = render_signal_report('2026-01-01', market, signals)
    assert '候选（1只' in text
    assert '测试股（300001）' in text and 'entry  5日线首动' in text
    assert '失效：收盘破10（区间下沿，明日MA5×0.99）' in text
    assert '依据：5日线买在明天的位置，破5日就是卖点' in text
    assert '池内票区（2只）：' in text
    assert '池内股（300002｜UP）' in text
    assert '点位：stop 10–10.2｜t买 10–10.2｜t卖 10–10.2' in text
    assert 'parent=entry｜策略=5日线首动' in text
    assert '延迟观察' in text
    assert '回踩失败' in text
    assert 'A0 信号日（2026-01-01）触发回顾' in text
    assert '观察股（300003）' in text
    assert '判决：测试股（300001）' in text
    assert '触发：测试股（300001） 止损（对应 池内）' in text
    assert '回执' not in text and '差异' not in text
    assert '放量突破' in text


def test_report_prefers_live_spot_price_over_signal_close():
    signals = [_signal('entry', SignalTag.ENTER, SignalSide.BUY)]
    market = {
        'name_map': {'300001': '测试股'},
        'tier': 'NORMAL',
        'sh_close': 3005,
        'sh_ma5': 3000,
        'assignments': [{'symbol': '300001', 'est_close': 10.1}],
        'spot_prices': {'300001': 11.2},
    }
    text = render_signal_report('2026-01-01', market, signals)
    assert '现价：11.2' in text
