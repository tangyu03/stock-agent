from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..core.auction import AuctionPattern
from ..models.signal import Signal, SignalSide, SignalStatus
import sqlite3
from typing import Any
from ..core.lifecycle import JournalEntry
from ..db.session import load_active_signals, save_journal, save_signal


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
def run_auction(
    conn: sqlite3.Connection,
    trade_date: str,
    pattern: AuctionPattern,
    now: datetime,
) -> dict[str, Any]:
    before = load_active_signals(conn, trade_date)
    after = apply_auction(before, pattern, now)
    invalidated = [signal for signal in after if signal.status is SignalStatus.INVALIDATED]
    for signal in invalidated:
        save_signal(conn, signal)
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status=SignalStatus.ACTIVE.value,
            to_status=SignalStatus.INVALIDATED.value,
            reason='auction_weak',
            created_at=now,
        ))
    conn.commit()
    return {'pattern': pattern.value, 'invalidated': [signal.id for signal in invalidated]}
