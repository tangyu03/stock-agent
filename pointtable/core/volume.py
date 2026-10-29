from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BreakQualifier(str, Enum):
    EXECUTE_NOW = 'EXECUTE_NOW'
    DELAY_OBSERVE = 'DELAY_OBSERVE'
    DIVERGENCE = 'DIVERGENCE'
    LIMIT_EXEMPT = 'LIMIT_EXEMPT'
    SAME_DAY_RECOVERED = 'SAME_DAY_RECOVERED'
    UNCLASSIFIED = 'UNCLASSIFIED'


class SettlementOutcome(str, Enum):
    EXECUTE_NOW = 'EXECUTE_NOW'
    STILL_BROKEN = 'STILL_BROKEN'
    WEAK_MARKET_EXECUTE = 'WEAK_MARKET_EXECUTE'
    SAME_DAY_RECOVERED = 'SAME_DAY_RECOVERED'


class Activity(str, Enum):
    ACTIVE = 'ACTIVE'
    PASSIVE = 'PASSIVE'


@dataclass(frozen=True)
class BreakDecision:
    qualifier: BreakQualifier
    reason: str
    data_status: str = 'OK'


def break_qualifier(
    volume_ratio: float | None,
    active_delta: float | None,
    divergence: bool = False,
    limit_up: bool = False,
) -> BreakDecision:
    '''Judge a support break with volume, active selling and divergence.'''
    if limit_up:
        return BreakDecision(BreakQualifier.LIMIT_EXEMPT, 'limit_up_price_distortion')
    if divergence:
        return BreakDecision(BreakQualifier.DIVERGENCE, 'divergence_needs_close')
    if volume_ratio is None:
        return BreakDecision(BreakQualifier.UNCLASSIFIED, 'volume_ratio_missing', 'MISSING')
    if volume_ratio < 0:
        raise ValueError('volume_ratio must not be negative')
    if active_delta is None:
        return BreakDecision(BreakQualifier.UNCLASSIFIED, 'active_delta_missing', 'MISSING')
    if volume_ratio >= 1.2 and active_delta is not None and active_delta > 0:
        return BreakDecision(BreakQualifier.EXECUTE_NOW, 'active_volume_break')
    return BreakDecision(BreakQualifier.DELAY_OBSERVE, 'passive_or_low_volume_break')


def active_or_passive(
    stock_triggered: bool,
    sector_triggered: bool,
    index_triggered: bool,
) -> Activity:
    '''Individual weakness with calm context is active; context damage is passive.'''
    if not stock_triggered:
        return Activity.PASSIVE
    if sector_triggered or index_triggered:
        return Activity.PASSIVE
    return Activity.ACTIVE


def is_circuit_day(
    market_tier: str,
    index_trend: str,
    *,
    volume_tier: str = 'LOW',
    index_trend_rule: str = 'DOWN',
) -> bool:
    return (
        market_tier.upper() == str(volume_tier).upper()
        and index_trend.upper() == str(index_trend_rule).upper()
    )


def resolve_effective_qualifier(
    raw_qualifier: str | None,
    circuit_day: bool,
) -> BreakQualifier:
    try:
        qualifier = BreakQualifier(str(raw_qualifier))
    except ValueError:
        return BreakQualifier.UNCLASSIFIED
    if qualifier is BreakQualifier.UNCLASSIFIED:
        return qualifier
    if circuit_day and qualifier in {BreakQualifier.DELAY_OBSERVE, BreakQualifier.DIVERGENCE}:
        return BreakQualifier.EXECUTE_NOW
    return qualifier
