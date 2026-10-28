from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BreakQualifier(str, Enum):
    EXECUTE_NOW = 'EXECUTE_NOW'
    DELAY_OBSERVE = 'DELAY_OBSERVE'
    DIVERGENCE = 'DIVERGENCE'
    LIMIT_EXEMPT = 'LIMIT_EXEMPT'
    SAME_DAY_RECOVERED = 'SAME_DAY_RECOVERED'


class SettlementOutcome(str, Enum):
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


def break_qualifier(
    volume_ratio: float,
    active_delta: float | None,
    divergence: bool = False,
    limit_up: bool = False,
) -> BreakDecision:
    '''Judge a support break with volume, active selling and divergence.'''
    if volume_ratio < 0:
        raise ValueError('volume_ratio must not be negative')
    if limit_up:
        return BreakDecision(BreakQualifier.LIMIT_EXEMPT, 'limit_up_price_distortion')
    if divergence:
        return BreakDecision(BreakQualifier.DIVERGENCE, 'divergence_needs_close')
    if active_delta is None:
        return BreakDecision(BreakQualifier.DELAY_OBSERVE, 'active_delta_missing')
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
