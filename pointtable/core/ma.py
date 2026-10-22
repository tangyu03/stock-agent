from __future__ import annotations

from collections.abc import Sequence


def ma_tomorrow(closes: Sequence[float], n: int, est_close: float) -> float:
    '''Return tomorrow's MA using a tail-session estimated close.'''
    if n < 1:
        raise ValueError('n must be positive')
    if len(closes) < n - 1:
        raise ValueError('closes must contain at least n-1 values')
    if est_close < 0:
        raise ValueError('est_close must not be negative')
    window = [*closes[-(n - 1):], est_close] if n > 1 else [est_close]
    if any(value < 0 for value in window):
        raise ValueError('closes must not be negative')
    return sum(window) / n
