from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any


class SectorState(str, Enum):
    MAINLINE = 'mainline'
    ROTATIONAL = 'rotational'
    RETREATING = 'retreating'


def load_theme_map(path: str | Path) -> dict[str, str]:
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    overrides = raw.get('overrides') or {}
    return {str(code).strip(): str(label).strip() for code, label in overrides.items() if label}


def classify_sector_state(
    five_day_return: float,
    rank_change: int,
    pool_trigger_count: int,
) -> SectorState:
    if five_day_return < 0 and rank_change <= 0:
        return SectorState.RETREATING
    if five_day_return > 0 and rank_change >= 0 and pool_trigger_count >= 2:
        return SectorState.MAINLINE
    return SectorState.ROTATIONAL


def order_pool(
    entries: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    '''Put unbroken, upstream and first-line pool members first.'''
    return sorted(
        entries,
        key=lambda row: (
            bool(row.get('broken', False)),
            not bool(row.get('upstream', False)),
            not bool(row.get('first_line', False)),
            str(row.get('symbol', '')),
        ),
    )
