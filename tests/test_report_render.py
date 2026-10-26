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
        _signal('entry', SignalTag.ENTER, SignalSide.BUY),
        _signal(
            'stop',
            SignalTag.STOP,
            SignalSide.SELL,
            strategy_id='holding',
                    symbol='300002',
            parent_id='pool:300001',
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
        'name_map': {'300001': '测试股', '300002': '保护股', '300003': '观察股'},
        'tier': 'LOW',
        'sh_close': 3005,
        'sh_ma5': 3000,
        'execution_review': [{
            'symbol': '300001',
            'strategy_id': 'holding',
            'tag': 'stop',
            'zone_low': 10.0,
            'zone_high': 10.2,
            'verdict': 'ignored',
            'difference': 'ignored',
        }],
        'ev_negative_strategies': ['volume_breakout'],
    }
    text = render_signal_report('2026-01-01', market, signals)
    assert '测试股（300001） 5日线首动' in text
    assert '保护股（300002）【止损】 持仓保护' in text
    assert '延迟观察' in text
    assert '回踩失败' in text
    assert '【今日执行回顾】：' in text
    assert '测试股（300001） 持仓保护 止损' in text
    assert '放量突破' in text
