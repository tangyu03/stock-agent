# -*- coding: utf-8 -*-
"""Market vs sector resonance observation.

This module deliberately does not change market mode or scheduling.  It only
explains whether sector breadth agrees with the index-level mode.
"""
from typing import Any, Dict, Iterable, Optional


def _class(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("classification") or "unknown")
    return str(getattr(value, "classification", None) or "unknown")


def assess_sector_resonance(
    market_mode: str,
    sectors: Iterable[Any],
    snapshot_date: str = "",
    lag_days: int = 0,
) -> Optional[Dict[str, Any]]:
    sector_list = list(sectors)
    total = len(sector_list)
    if total == 0:
        return None

    counts = {"main_trend": 0, "rotational": 0, "retreating": 0, "unknown": 0}
    for sector in sector_list:
        counts[_class(sector)] = counts.get(_class(sector), 0) + 1

    main = counts["main_trend"]
    retreat = counts["retreating"]
    mode = str(market_mode or "").lower()
    if mode in ("attack", "offense", "offensive"):
        direction = "attack"
    elif mode in ("defend", "defense", "defensive"):
        direction = "defend"
    elif mode in ("retreat", "defensive_retreat"):
        direction = "defend"
    else:
        direction = "neutral"

    if main > retreat:
        relation = "bullish"
    elif retreat > main:
        relation = "bearish"
    else:
        relation = "balanced"

    if direction == "attack" and relation == "bullish":
        status = "bullish_resonance"
    elif direction == "attack" and relation == "bearish":
        status = "bearish_divergence"
    elif direction == "defend" and relation == "bearish":
        status = "bearish_resonance"
    elif direction == "defend" and relation == "bullish":
        status = "bullish_divergence"
    else:
        status = "mixed"

    line = (
        f"共振观测: 指数{mode} | 行业主线{main}({main / total:.0%}) | "
        f"退潮{retreat}({retreat / total:.0%}) | {status}"
    )
    if int(lag_days) > 0:
        line += f" | 快照滞后{int(lag_days)}天,仅观测不投票"
    elif snapshot_date:
        line += f" | 快照{snapshot_date},仅观测不投票"

    mode_label = {"attack": "进攻", "defend": "防守"}.get(mode, mode or "未知")
    line = line.replace(f"指数{mode}", f"指数{mode_label}", 1)

    return {
        "available": int(lag_days) == 0,
        "status": status,
        "market_mode": mode,
        "snapshot_date": snapshot_date,
        "lag_days": int(lag_days),
        "total": total,
        "counts": counts,
        "line": line,
    }
