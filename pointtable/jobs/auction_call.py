from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..core.auction import AuctionPattern
from ..models.signal import Signal, SignalSide, SignalStatus


def apply_auction(
    signals: Iterable[Signal],
    pattern: AuctionPattern,
    now: datetime | None = None,
) -> list[Signal]:
    if pattern is not AuctionPattern.STRONG_TO_WEAK:
        return list(signals)
    closed_at = now or datetime.now().astimezone()
    return [
        signal.model_copy(update={
            'status': SignalStatus.INVALIDATED,
            'status_reason': 'auction_weak',
            'closed_at': closed_at,
        })
        if signal.side is SignalSide.BUY and signal.status is SignalStatus.ACTIVE
        else signal
        for signal in signals
    ]
