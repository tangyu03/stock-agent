"""Single source for market mode disclosure and panic wording."""

from typing import Dict, List, Optional


MODE_NAMES = {
    "attack": "进攻",
    "defend": "防守",
    "retreat": "撤退",
    "panic": "恐慌",
}


def _number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_market_panic(
    index_drop=None,
    gem_star_drop=None,
    ad_ratio=None,
    cum_drop_5d=None,
    down_days_5d=None,
    cum_drop_threshold=6.0,
    cum_down_days_threshold=3,
) -> Dict:
    """Evaluate the market-side panic precondition used by both layers."""
    index_value = _number(index_drop)
    gem_value = _number(gem_star_drop)
    ad_value = _number(ad_ratio)
    cum_value = _number(cum_drop_5d)
    down_days_value = _number(down_days_5d)

    reasons: List[str] = []
    if index_value is not None and abs(index_value) > 4.0:
        reasons.append(f"指数跌幅{abs(index_value):.2f}%>4%")
    if gem_value is not None and abs(gem_value) > 5.0:
        reasons.append(f"双创跌幅{abs(gem_value):.2f}%>5%")
    if ad_value is not None and ad_value < 0.15:
        reasons.append(f"涨跌比{ad_value:.2f}<0.15")
    if (
        cum_value is not None
        and abs(cum_value) > cum_drop_threshold
        and down_days_value is not None
        and down_days_value >= cum_down_days_threshold
    ):
        reasons.append(
            f"连{cum_down_days_threshold:.0f}日阴累计{abs(cum_value):.2f}%"
        )

    missing = [
        name
        for name, value in (
            ("指数跌幅", index_value),
            ("双创跌幅", gem_value),
            ("涨跌比", ad_value),
        )
        if value is None
    ]
    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "thresholds": {
            "index_drop": ">4.0%",
            "gem_star_drop": ">5.0%",
            "ad_ratio": "<0.15",
            "cum_drop_5d": f">{cum_drop_threshold:.0f}%",
            "cum_down_days_5d": f">={cum_down_days_threshold:.0f}",
        },
        "missing": missing,
    }


def build_mode_transition_matrix(current_mode: str) -> List[Dict]:
    """Disclose the live MA5 mode machine and downgrade edges."""
    rows = [
        {
            "mode": "attack",
            "label": "进攻",
            "entry": "收盘>MA5 且 MA5>MA10>MA20 且 派发日≤5",
            "exit": "条件破坏；外盘或预期落空可降一档",
        },
        {
            "mode": "defend",
            "label": "防守",
            "entry": "未满足进攻且未跌破MA5×0.99；派发数据过期也强制防守",
            "exit": "进入进攻条件或跌破MA5×0.99；外盘或预期落空可降一档",
        },
        {
            "mode": "retreat",
            "label": "撤退",
            "entry": "收盘<MA5×0.99（跌破5日线且带1%缓冲）",
            "exit": "收盘收复MA5×0.99",
        },
    ]
    for row in rows:
        row["is_current"] = row["mode"] == current_mode
    return rows


def render_market_panic_line(panic: Optional[Dict]) -> str:
    if not panic:
        return "恐慌口径: 数据未取到"
    label = "已触发" if panic.get("triggered") else "未触发"
    reasons = "；".join(str(item) for item in panic.get("reasons", []))
    missing = "；".join(str(item) for item in panic.get("missing", []))
    detail = reasons or ("数据未取到:" + ",".join(missing) if missing else "均未达阈值")
    return f"恐慌口径: {label}({detail})；恐慌抄底仍需充分条件+个股超卖"


def mode_label(mode: str) -> str:
    return MODE_NAMES.get(str(mode or ""), str(mode or ""))
