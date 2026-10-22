from datetime import UTC, datetime

from pointtable.jobs.restore_check import restore_reminders
from pointtable.models.signal import Signal, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _stop(status):
    return Signal(
        id='stop', symbol='300001', strategy_id='s', side='SELL', tag=SignalTag.STOP,
        zone_low=10.0, zone_high=10.2, trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED, status=status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), parent_id='enter',
    )


def test_recovery_is_reminder_only():
    rows = restore_reminders([_stop(SignalStatus.TRIGGERED)], 10.1)
    assert len(rows) == 1
    assert rows[0].status is SignalStatus.TRIGGERED
    assert restore_reminders([_stop(SignalStatus.ACTIVE)], 10.1) == []
