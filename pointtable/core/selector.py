from __future__ import annotations

from collections.abc import Mapping


MA_FAMILY = {
    'first_move_5d': 'ma5',
    'first_move_drift': 'ma5',
    'first_move_10d': 'ma10',
}

COOLDOWN_STOP_COUNT = 2


def check_first_move_5d(facts: Mapping[str, object]) -> bool:
    return bool(facts.get('ma5_rising'))


def check_first_move_10d(facts: Mapping[str, object]) -> bool:
    return bool(facts.get('deep_pullback') and facts.get('ma10_alive'))


def check_first_move_drift(facts: Mapping[str, object]) -> bool:
    return bool(facts.get('ma5_falling') and facts.get('drift_breakout'))


def check_first_board_dip(facts: Mapping[str, object]) -> bool:
    return bool(facts.get('first_board') and facts.get('near_support'))


def check_volume_breakout(facts: Mapping[str, object]) -> bool:
    return bool(facts.get('volume_breakout'))


CHECKS = {
    'first_move_5d': check_first_move_5d,
    'first_move_10d': check_first_move_10d,
    'first_move_drift': check_first_move_drift,
    'first_board_dip': check_first_board_dip,
    'volume_breakout': check_volume_breakout,
}


def select_strategies(facts: Mapping[str, object]) -> list[str]:
    if not all(bool(facts.get(key)) for key in ('hot_theme', 'not_regulated', 'lhb_ok')):
        return []
    return [strategy_id for strategy_id, check in CHECKS.items() if check(facts)]


def cooled_out(strategy_ids: list[str], stop_counts: Mapping[str, int], cooldown_days: int) -> list[str]:
    if cooldown_days < 0:
        raise ValueError('cooldown_days must not be negative')
    return [
        strategy_id
        for strategy_id in strategy_ids
        if stop_counts.get(MA_FAMILY.get(strategy_id, strategy_id), 0) < COOLDOWN_STOP_COUNT
    ]
