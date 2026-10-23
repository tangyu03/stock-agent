from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum

from ..core.calendar import is_trading_day


class Routine(str, Enum):
    AUCTION = 'auction'
    WATCH = 'watch'
    TAIL = 'tail'
    RESTORE = 'restore'
    SETTLE = 'settle'
    EVENING = 'evening'


FIXED_TIMES = {
    Routine.AUCTION: time(9, 25),
    Routine.WATCH: time(9, 30),
    Routine.TAIL: time(14, 30),
    Routine.RESTORE: time(14, 31),
    Routine.SETTLE: time(15, 5),
    Routine.EVENING: time(15, 30),
}


@dataclass(frozen=True)
class ScheduledRoutine:
    routine: Routine
    run_at: datetime


def next_routine(now: datetime, holidays: set | None = None) -> ScheduledRoutine:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    candidate_date = now.date()
    while True:
        if is_trading_day(candidate_date, holidays):
            for routine in Routine:
                scheduled = datetime.combine(
                    candidate_date, FIXED_TIMES[routine], tzinfo=now.tzinfo
                )
                if scheduled > now:
                    return ScheduledRoutine(routine, scheduled)
        candidate_date += timedelta(days=1)
