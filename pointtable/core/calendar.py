from __future__ import annotations

from datetime import date, timedelta


def load_holidays(raw: object) -> set[date]:
    holidays: set[date] = set()
    for item in raw or ():
        if isinstance(item, date):
            holidays.add(item)
            continue
        try:
            holidays.add(date.fromisoformat(str(item)))
        except ValueError:
            continue
    return holidays


def is_trading_day(value: date, holidays: set[date] | None = None) -> bool:
    return value.weekday() < 5 and value not in (holidays or set())


def trading_days_between(start: date, end: date, holidays: set[date] | None = None) -> int:
    if end <= start:
        return 0
    return sum(
        is_trading_day(current, holidays)
        for current in (start + timedelta(days=offset) for offset in range(1, (end - start).days + 1))
    )
