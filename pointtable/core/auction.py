from __future__ import annotations

from enum import Enum


class AuctionPattern(str, Enum):
    WEAK_TO_STRONG = 'weak_to_strong'
    STRONG_TO_WEAK = 'strong_to_weak'
    NEUTRAL = 'neutral'
    ONE_WORD_OPEN = 'one_word_open'


def classify_auction(
    pre_close: float,
    auction_price: float,
    volume_ratio: float,
) -> AuctionPattern:
    if pre_close <= 0 or auction_price <= 0 or volume_ratio < 0:
        raise ValueError('prices must be positive and volume_ratio must not be negative')
    if auction_price < pre_close and volume_ratio >= 1.0:
        return AuctionPattern.WEAK_TO_STRONG
    if auction_price >= pre_close * 1.098:
        return AuctionPattern.ONE_WORD_OPEN
    if auction_price > pre_close and volume_ratio < 0.8:
        return AuctionPattern.STRONG_TO_WEAK
    return AuctionPattern.NEUTRAL
