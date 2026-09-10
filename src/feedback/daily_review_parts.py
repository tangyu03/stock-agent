"""Pure builders for the daily review tail and environment invalidation."""

from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

from .observation_tracker import get_observation_intercepts


def previous_weekday(as_of: Optional[date] = None) -> str:
    current = as_of or date.today()
    current -= timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


def build_yesterday_observation_review(
    track_date: str,
    prices: Dict[str, float],
    benchmark_change_pct: Optional[float] = None,
) -> str:
    """One-line T0 vs T+1 comparison; missing quotes stay explicit."""
    rows = get_observation_intercepts(track_date=track_date)
    if not rows:
        return f"昨日观察今日表现: {track_date}无拦截票记录"
    parts: List[str] = []
    changes: List[float] = []
    for row in rows:
        code = str(row["stock_code"])
        name = row["stock_name"] or code
        t0_close = float(row.get("t0_close") or 0)
        current = float(prices.get(code) or 0)
        if t0_close <= 0 or current <= 0:
            parts.append(f"{name}({code})数据未取到")
            continue
        change = (current / t0_close - 1.0) * 100
        changes.append(change)
        parts.append(f"{name}({code}){change:+.1f}%")
    if not changes:
        return f"昨日观察今日表现: {' | '.join(parts)} | 基准数据未取到"
    average = sum(changes) / len(changes)
    benchmark_text = (
        f"(vs 沪深300 {benchmark_change_pct:+.1f}%)"
        if benchmark_change_pct is not None else "(vs 沪深300 数据未取到)"
    )
    return (
        f"昨日观察今日表现: {' | '.join(parts)} | "
        f"观察票均值{average:+.1f}% {benchmark_text}"
    )


def evaluate_environment_invalidation(environment: Dict) -> Dict:
    """Machine-check the declared invalidation rule; missing data never passes."""
    market_env = environment.get("market_env") or {}
    gem = (environment.get("gem_sci_tech") or {}).get("gem") or {}

    def _number(value) -> Optional[float]:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result

    ad_ratio = _number(market_env.get("ad_ratio"))
    volume_ratio = _number(market_env.get("turnover_ratio"))
    gem_close = _number(gem.get("current"))
    gem_ma20 = _number(gem.get("ma20"))

    checks = {
        "涨跌比": ad_ratio is not None and ad_ratio >= 1.2,
        "量比": volume_ratio is not None and volume_ratio >= 1.0,
        "创业板收复MA20": (
            gem_close is not None and gem_ma20 is not None
            and gem_close >= gem_ma20
        ),
    }
    missing = {
        "涨跌比": ad_ratio is None,
        "量比": volume_ratio is None,
        "创业板收复MA20": gem_close is None or gem_ma20 is None,
    }
    details = {
        "ad_ratio": ad_ratio,
        "volume_ratio": volume_ratio,
        "gem_close": gem_close,
        "gem_ma20": gem_ma20,
    }
    return {
        "checks": checks,
        "missing": missing,
        "details": details,
        "triggered": all(checks.values()),
    }


def format_invalidation_clause(environment: Dict) -> str:
    result = evaluate_environment_invalidation(environment)
    details = result["details"]

    def _text(value, threshold=None):
        if value is None:
            return "数据未取到"
        if threshold is None:
            return f"{value:.2f}"
        return f"{value:.2f}/{threshold:.2f}"

    missing_items = [
        name for name, is_missing in result["missing"].items() if is_missing
    ]
    check_items = [
        f"涨跌比{_text(details['ad_ratio'], 1.2)}{'✓' if result['checks']['涨跌比'] else '✗'}",
        f"量比{_text(details['volume_ratio'], 1.0)}{'✓' if result['checks']['量比'] else '✗'}",
        (
            f"创业板{details['gem_close'] if details['gem_close'] is not None else '数据未取到'}"
            f"/MA20{details['gem_ma20'] if details['gem_ma20'] is not None else '数据未取到'}"
            f"{'✓' if result['checks']['创业板收复MA20'] else '✗'}"
        ),
    ]
    lines = [
        "本判定失效条件: 明日收盘 涨跌比≥1.2 且 量比≥1.0 且 创业板收复MA20，"
        "三项同时满足 → 环境判定作废重估",
        "失效条款机检: " + " | ".join(check_items),
    ]
    if missing_items:
        lines.append("失效条款未完整机检: 缺" + "+".join(missing_items))
    if result["triggered"]:
        lines.append("⚠环境判定作废重估告警")
    return "\n".join(lines)
