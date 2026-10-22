import pytest
from datetime import date, datetime, timezone

from pointtable.models import (
    JournalEntry,
    PoolEntry,
    ReceiptEntry,
    Signal,
    SignalSide,
    SignalStatus,
    SignalTag,
    SignalTrend,
    SignalValidPolicy,
)


def test_signal_requires_zone_and_parent_for_dependent_tags():
    base = {
        "id": "s1",
        "symbol": "300001",
        "strategy_id": "volume_breakout",
        "side": SignalSide.BUY,
        "tag": SignalTag.ENTER,
        "zone_low": 10.0,
        "zone_high": 11.0,
        "trend": SignalTrend.UP,
        "valid_policy": SignalValidPolicy.INTRADAY,
        "created_at": datetime.now(timezone.utc),
    }
    assert Signal(**base).tag == SignalTag.ENTER
    with pytest.raises(ValueError):
        Signal(**{**base, "tag": SignalTag.STOP})


def test_signal_terminal_status_requires_closed_at():
    with pytest.raises(ValueError):
        Signal(
            id="s2",
            symbol="300001",
            strategy_id="volume_breakout",
            side=SignalSide.SELL,
            tag=SignalTag.STOP,
            zone_low=10.0,
            zone_high=11.0,
            trend=SignalTrend.DOWN,
            valid_policy=SignalValidPolicy.UNTIL_STOPPED,
            status=SignalStatus.EXPIRED,
            parent_id="s1",
            created_at=datetime.now(timezone.utc),
        )


def test_journal_and_receipt_models_are_immutable_by_contract():
    now = datetime.now(timezone.utc)
    journal = JournalEntry(signal_id="s1", from_status="ACTIVE", to_status="TRIGGERED", created_at=now)
    receipt = ReceiptEntry(signal_id="s1", verdict="executed", created_at=now)
    assert journal.confirmed.value == "touched_only"
    assert receipt.verdict.value == "executed"


def test_pool_requires_theme_label():
    entry = PoolEntry(entry_date=date.today(), symbol="300001", theme_label="存储")
    assert entry.theme_label == "存储"