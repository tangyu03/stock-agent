from datetime import UTC, datetime

from pointtable.core.auction import AuctionPattern
from pointtable.jobs.auction_call import apply_auction
from pointtable.jobs.settle import create_retest_confirm, expire_intraday
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


def test_buy_and_stop_are_single_point():
    assert confirm_touch(_signal(), 10.5)[1] == 'entry_single_point'
    assert confirm_touch(_signal(SignalTag.STOP, parent_id='evt-0'), 10.5)[1] == 'stop_single_point'
    assert confirm_touch(_signal(), 12.0)[1] == 'not_in_zone'


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
    assert retest.parent_id == (stop.parent_id or stop.id)


def test_next_day_survives_generation_day_and_expires_next_day():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    next_day = _signal(policy=SignalValidPolicy.NEXT_DAY)
    assert expire_intraday([next_day], now)[0].status is SignalStatus.ACTIVE
    later = next_day.model_copy(update={'created_at': datetime(2026, 1, 2, tzinfo=UTC)})
    assert expire_intraday([later], datetime(2026, 1, 3, tzinfo=UTC))[0].status is SignalStatus.EXPIRED


def test_same_day_t_expires_at_settle():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    row = _signal(SignalTag.T, SignalValidPolicy.SAME_DAY, parent_id='evt-0').model_copy(update={
        'created_at': datetime(2026, 1, 1, tzinfo=UTC),
    })
    assert expire_intraday([row], now)[0].status is SignalStatus.EXPIRED
