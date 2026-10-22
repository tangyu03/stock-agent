from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LhbDecision:
    bad: bool
    reason: str


def evaluate_lhb(
    dumper_hits: set[str],
    seller_concentration: float,
    concentration_threshold: float = 0.35,
) -> LhbDecision:
    if seller_concentration < 0 or seller_concentration > 1:
        raise ValueError('seller_concentration must be between 0 and 1')
    if concentration_threshold <= 0 or concentration_threshold > 1:
        raise ValueError('concentration_threshold must be between 0 and 1')
    if dumper_hits:
        return LhbDecision(True, 'dumper_hit:' + ','.join(sorted(dumper_hits)))
    if seller_concentration >= concentration_threshold:
        return LhbDecision(True, 'seller_concentration')
    return LhbDecision(False, 'ok')
