from datetime import UTC, datetime

from pointtable.core.auction import AuctionPattern
from pointtable.jobs.auction_call import apply_auction
from pointtable.jobs.settle import apply_trend_flip, create_retest_confirm, expire_intraday
from pointtable.jobs.watch import confirm_touch
from pointtable.models.signal import (
    Signal, SignalSide, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy,
)


def _signal(tag=SignalTag.ENTER, policy=SignalValidPolicy.INTRADAY, parent_id=None):
    return Signal(
        id='evt-1', symbol='300001', strategy_id='s', side='BUY', tag=tag,
        zone_low=10.0, zone_high=11.0, trend=SignalTrend.UP,
        valid_policy=policy, created_at=datetime(2026, 1, 1, tzinfo=UTC), parent_id=parent_id,
    )


def test_strong_to_weak_invalidates_active_buys():
    row = _signal()
    rows = apply_auction([row], AuctionPattern.STRONG_TO_WEAK)
    assert rows[0].status is SignalStatus.INVALIDATED
    assert rows[0].status_reason == 'auction_weak'


def test_buy_needs_second_cycle_but_stop_is_single_point():
    assert confirm_touch(_signal(), 10.5, False)[1] == 'waiting_second_cycle'
    assert confirm_touch(_signal(), 10.5, True)[1] == 'buy_confirmed'
    assert confirm_touch(_signal(SignalTag.STOP, parent_id='evt-0'), 10.5, False)[1] == 'stop_single_point'
    assert confirm_touch(_signal(), 12.0, True)[1] == 'not_in_zone'


def test_settle_expires_intraday_and_creates_retest():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    intraday = _signal()
    stopped = _signal(policy=SignalValidPolicy.UNTIL_STOPPED)
    rows = expire_intraday([intraday, stopped], now)
    assert rows[0].status is SignalStatus.EXPIRED
    assert rows[1].status is SignalStatus.ACTIVE
    stop = rows[0].model_copy(update={'tag': SignalTag.STOP, 'side': 'SELL'})
    retest = create_retest_confirm(stop, (10.0, 10.2), now)
    assert retest.tag is SignalTag.RETEST_CONFIRM
    assert retest.parent_id == stop.id


def test_trend_flip_rolls_stop_and_invalidates_t_buy():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    stop = _signal(SignalTag.STOP, SignalValidPolicy.UNTIL_STOPPED, 'evt-0')
    t_buy = _signal(SignalTag.T, parent_id='evt-0')
    t_sell = _signal(SignalTag.T, parent_id='evt-0').model_copy(update={'side': SignalSide.SELL})
    rows = apply_trend_flip([stop, t_buy, t_sell], (9.0, 9.2), now)
    assert (rows[0].zone_low, rows[0].zone_high) == (9.0, 9.2)
    assert rows[1].status is SignalStatus.INVALIDATED
    assert rows[1].status_reason == 'trend_flip'
    assert rows[1].closed_at == now
    assert rows[2].status is SignalStatus.ACTIVE
