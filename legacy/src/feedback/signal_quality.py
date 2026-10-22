"""Strategy quality cards from the signal-only ledger."""

from typing import Dict, List, Optional

from .signal_ledger import get_signal_ledger


def _design_payoff(row: Dict) -> Optional[float]:
    try:
        buy = float(row.get("buy_price") or 0)
        stop = float(row.get("stop_loss") or 0)
        target = float(row.get("target_low") or row.get("target_high") or 0)
    except (TypeError, ValueError):
        return None
    if buy <= 0 or stop <= 0 or target <= 0 or buy - stop <= 0:
        return None
    return (target - buy) / (buy - stop)


def build_signal_quality_stats(
    rows: Optional[List[Dict]] = None,
    target: int = 30,
) -> List[Dict]:
    """Count completed signals: target=win, stop=loss, expiry/time=flat."""
    if rows is None:
        rows = get_signal_ledger()
    buckets: Dict[str, Dict] = {}
    for row in rows or []:
        strategy = str(row.get("strategy") or "未知策略")
        status = str(row.get("completion_status") or "")
        if status not in ("sig_target", "sig_stop", "expired", "time_exit"):
            continue
        bucket = buckets.setdefault(strategy, {
            "strategy": strategy, "sample": 0,
            "target": 0, "stop": 0, "flat": 0,
            "payoffs": [],
        })
        bucket["sample"] += 1
        if status == "sig_target":
            bucket["target"] += 1
        elif status == "sig_stop":
            bucket["stop"] += 1
        else:
            bucket["flat"] += 1
        payoff = _design_payoff(row)
        if payoff is not None:
            bucket["payoffs"].append(payoff)

    result = []
    for bucket in buckets.values():
        n = bucket["sample"]
        payoff = (
            sum(bucket["payoffs"]) / len(bucket["payoffs"])
            if bucket["payoffs"] else None
        )
        result.append({
            "strategy": bucket["strategy"],
            "sample": n,
            "target": bucket["target"],
            "stop": bucket["stop"],
            "flat": bucket["flat"],
            "target_pct": bucket["target"] / n * 100,
            "stop_pct": bucket["stop"] / n * 100,
            "flat_pct": bucket["flat"] / n * 100,
            "design_payoff": payoff,
            "insufficient": n < target,
            "target_sample": target,
        })
    return sorted(result, key=lambda item: item["sample"], reverse=True)


def format_signal_quality_card(stats: Dict, signal_id: str = "") -> str:
    lead = f"#{signal_id} " if signal_id else ""
    payoff = stats.get("design_payoff")
    payoff_text = f"{payoff:.2f}" if payoff is not None else "无有效价位"
    prefix = "样本不足" if stats.get("insufficient") else "历史样本"
    return (
        f"{lead}{stats['strategy']} | {prefix}({stats['sample']}/"
        f"{stats.get('target_sample', 30)}): "
        f"触目标{stats['target_pct']:.0f}% 触止损{stats['stop_pct']:.0f}% "
        f"到期{stats['flat_pct']:.0f}% | 设计盈亏比{payoff_text}"
    )


def build_signal_quality_cards(rows: Optional[List[Dict]] = None) -> List[Dict]:
    return [
        {
            "strategy": stat["strategy"],
            "card": format_signal_quality_card(stat),
        }
        for stat in build_signal_quality_stats(rows)
    ]
