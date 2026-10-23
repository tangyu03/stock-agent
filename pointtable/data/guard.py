from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
import math
import math


def _parse_date(value: Any) -> date | None:
    text = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def find_recent_trading_day(target: date, skip_today: bool = False) -> date:
    start = 1 if skip_today else 0
    for offset in range(start, 11):
        candidate = target - timedelta(days=offset)
        if candidate.weekday() < 5:
            return candidate
    return target


class DataFreshnessGuard:
    def __init__(self, max_staleness_days: int = 5):
        self.max_staleness_days = max_staleness_days

    def check_source(
        self,
        rows: Iterable[Mapping[str, Any]],
        field: str = 'trade_date',
        now: date | None = None,
        skip_today: bool = False,
    ) -> bool:
        dates = [value for value in (_parse_date(row.get(field)) for row in rows) if value]
        if not dates:
            return False
        expected = find_recent_trading_day(now or date.today(), skip_today=skip_today)
        return max(dates) == expected

    def check_fresh_enough(self, latest: Any, now: date | None = None) -> bool:
        parsed = _parse_date(latest)
        if parsed is None:
            return False
        cutoff = (now or date.today()) - timedelta(days=self.max_staleness_days)
        return parsed >= cutoff


@dataclass(frozen=True)
class DirtyDataDecision:
    ok: bool
    reason: str = ''


def check_volume_caliber(
    volume_ratio: float | None,
    volume_vs_ma60: float | None,
    turnover_rate: float | None = None,
    volume_shares: float | None = None,
    kline_volume_ratio: float | None = None,
    *,
    turnover_threshold_pct: float = 10.0,
    max_volume_shares: float = 1_000_000_000.0,
    cross_caliber_tolerance: float = 0.25,
) -> DirtyDataDecision:
    '''Reject impossible volume/turnover units before a point is generated.'''
    if volume_ratio is not None and volume_ratio < 0:
        return DirtyDataDecision(False, 'negative_volume_ratio')
    if volume_ratio is not None and volume_vs_ma60 is not None and volume_ratio < 1 and volume_vs_ma60 > 10:
        return DirtyDataDecision(False, 'volume_ratio_ma60_conflict')
    if (
        turnover_rate is not None
        and volume_shares is not None
        and turnover_rate < turnover_threshold_pct
        and volume_shares > max_volume_shares
    ):
        return DirtyDataDecision(False, 'turnover_volume_conflict')
    if volume_ratio is not None and kline_volume_ratio is not None and volume_ratio > 0:
        deviation = abs(kline_volume_ratio - volume_ratio) / volume_ratio
        if deviation > cross_caliber_tolerance:
            return DirtyDataDecision(False, 'volume_caliber_cross_check')
    return DirtyDataDecision(True)


def check_quote_sanity(
    price: float | None,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    prev_close: float | None = None,
) -> DirtyDataDecision:
    values = [price, open_price, high, low, prev_close]
    if any(value is None for value in (price, high, low)):
        return DirtyDataDecision(False, 'missing_quote')
    if any(value is not None and (not math.isfinite(float(value)) or float(value) <= 0) for value in values):
        return DirtyDataDecision(False, 'invalid_number')
    price_f, high_f, low_f = float(price), float(high), float(low)
    if high_f < low_f:
        return DirtyDataDecision(False, 'high_low_conflict')
    if not low_f <= price_f <= high_f:
        return DirtyDataDecision(False, 'price_outside_range')
    if open_price is not None and not low_f <= float(open_price) <= high_f:
        return DirtyDataDecision(False, 'open_outside_range')
    if prev_close is not None:
        change_pct = abs(price_f / float(prev_close) - 1) * 100
        if change_pct > 20:
            return DirtyDataDecision(False, 'price_change_exceeds_limit')
    return DirtyDataDecision(True)


def check_two_source(
    primary: float | None,
    secondary: float | None,
    *,
    max_deviation: float = 0.005,
) -> DirtyDataDecision:
    if primary is None or secondary is None:
        return DirtyDataDecision(False, 'missing_source')
    primary_f, secondary_f = float(primary), float(secondary)
    if not math.isfinite(primary_f) or not math.isfinite(secondary_f) or primary_f <= 0 or secondary_f <= 0:
        return DirtyDataDecision(False, 'invalid_source_value')
    deviation = abs(primary_f - secondary_f) / max(primary_f, secondary_f)
    if deviation > max_deviation:
        return DirtyDataDecision(False, 'two_source_deviation')
    return DirtyDataDecision(True)


def check_quote_sanity(
    price: float | None,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    prev_close: float | None = None,
) -> DirtyDataDecision:
    values = [price, open_price, high, low, prev_close]
    if any(value is None for value in (price, high, low)):
        return DirtyDataDecision(False, 'missing_quote')
    if any(value is not None and (not math.isfinite(float(value)) or float(value) <= 0) for value in values):
        return DirtyDataDecision(False, 'invalid_number')
    price_f, high_f, low_f = float(price), float(high), float(low)
    if high_f < low_f:
        return DirtyDataDecision(False, 'high_low_conflict')
    if not low_f <= price_f <= high_f:
        return DirtyDataDecision(False, 'price_outside_range')
    if open_price is not None and not low_f <= float(open_price) <= high_f:
        return DirtyDataDecision(False, 'open_outside_range')
    if prev_close is not None:
        change_pct = abs(price_f / float(prev_close) - 1) * 100
        if change_pct > 20:
            return DirtyDataDecision(False, 'price_change_exceeds_limit')
    return DirtyDataDecision(True)


def check_two_source(
    primary: float | None,
    secondary: float | None,
    *,
    max_deviation: float = 0.005,
) -> DirtyDataDecision:
    if primary is None or secondary is None:
        return DirtyDataDecision(False, 'missing_source')
    primary_f, secondary_f = float(primary), float(secondary)
    if not math.isfinite(primary_f) or not math.isfinite(secondary_f) or primary_f <= 0 or secondary_f <= 0:
        return DirtyDataDecision(False, 'invalid_source_value')
    deviation = abs(primary_f - secondary_f) / max(primary_f, secondary_f)
    if deviation > max_deviation:
        return DirtyDataDecision(False, 'two_source_deviation')
    return DirtyDataDecision(True)
