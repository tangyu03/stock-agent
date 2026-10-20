"""A-share trading calendar with holiday support.

Loads known market holidays from config/trading_days.json.
Falls back to weekday-only check for years not in the config file.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HOLIDAYS_CACHE: Optional[dict] = None


def _load_holidays() -> dict:
    global _HOLIDAYS_CACHE
    if _HOLIDAYS_CACHE is not None:
        return _HOLIDAYS_CACHE
    path = Path(__file__).parent.parent.parent / "config" / "trading_days.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                _HOLIDAYS_CACHE = json.load(f)
        except Exception as e:
            logger.warning("[TradingCalendar] failed to load holidays: %s", e)
            _HOLIDAYS_CACHE = {}
    else:
        _HOLIDAYS_CACHE = {}
    return _HOLIDAYS_CACHE


def is_holiday(dt: datetime) -> bool:
    year = str(dt.year)
    holidays = _load_holidays()
    date_str = dt.strftime("%Y-%m-%d")
    return date_str in holidays.get(year, [])


def is_trading_day(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return not is_holiday(dt)


def find_recent_trading_day(target_date_str: str, max_lookback: int = 15, skip_today: bool = False) -> str:
    dt = None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(str(target_date_str).strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return str(target_date_str)
    start = 1 if skip_today else 0
    for i in range(start, max_lookback + start):
        d = dt - timedelta(days=i)
        if is_trading_day(d):
            return d.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")
