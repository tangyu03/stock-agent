from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from ..models.signal import SignalTrend


def moving_average(values: Sequence[float], n: int) -> float | None:
    if n < 1 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def classify_trend(closes: Sequence[float]) -> SignalTrend:
    '''UP requires close above MA20 and a rising 5-day MA.'''
    ma20 = moving_average(closes, 20)
    ma5 = moving_average(closes, 5)
    previous_ma5 = moving_average(closes[:-1], 5)
    if ma20 is None or ma5 is None or previous_ma5 is None:
        return SignalTrend.DOWN
    if closes[-1] > ma20 and ma5 > previous_ma5:
        return SignalTrend.UP
    return SignalTrend.DOWN
