from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


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
