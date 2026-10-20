"""Lightweight intraday T+0 decision helpers."""

from typing import Dict, Optional

from ..data_layer.stock_data import detect_support_resistance
from .timing_utils import ma20_rising


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _t0_shares(holding_shares: int, ratio: float) -> int:
    return max(100, int(holding_shares * ratio) // 100 * 100)


def build_t0_signal(
    holding: Dict,
    tech_data: Dict,
    market_mode: str,
    sector_status: str = "rotational",
) -> Optional[Dict]:
    """Build a reversible intraday T plan from support/resistance structure.

    Low buys are only allowed in an uptrend. In a downtrend, support is a risk
    line; only the two reverse-T variants near resistance are produced.
    """
    holding_shares = int(holding.get("shares") or 0)
    current_price = _number(tech_data.get("current_price"))
    if holding_shares <= 0 or not current_price or current_price <= 0:
        return None

    ma5 = _number(tech_data.get("ma5"))
    ma10 = _number(tech_data.get("ma10"))
    ma20 = _number(tech_data.get("ma20"))
    if not all([ma5, ma10, ma20]):
        return None

    trend_up = ma5 > ma10 > ma20 and ma20_rising(tech_data)
    trend_down = (ma5 < ma10 < ma20) or tech_data.get("ma20_falling") is True
    volume_ratio = _number(tech_data.get("volume_ratio")) or 1.0
    gain_20d = _number(tech_data.get("gain_20d")) or 0.0

    structure = detect_support_resistance(tech_data.get("kline") or [], window=20)
    support = next(
        (level for level in map(_number, structure.get("supports") or [])
         if level is not None and level < current_price),
        None,
    )
    resistance = next(
        (level for level in map(_number, structure.get("resistances") or [])
         if level is not None and level > current_price),
        None,
    )

    code = str(holding.get("code") or "")
    name = str(holding.get("stock_name") or code)
    cost = _number(holding.get("cost_price")) or 0.0
    base = {
        "stock_code": code,
        "stock_name": name,
        "holding_shares": holding_shares,
        "cost": cost,
    }

    if (
        support
        and trend_up
        and market_mode != "retreat"
        and sector_status != "retreating"
        and support * 0.98 <= current_price <= support * 1.02
    ):
        if volume_ratio < 1.0:
            signal_type = "正T-加仓低吸"
            ratio = 0.20
        elif volume_ratio < 1.5:
            signal_type = "正T-正常低吸"
            ratio = 0.10
        else:
            signal_type = "正T-低比例低吸"
            ratio = 0.05
        return {
            **base,
            "signal_type": signal_type,
            "direction": "正T",
            "price_range": [round(support, 2), round(current_price, 2)],
            "t0_shares": _t0_shares(holding_shares, ratio),
            "position_ratio": f"{ratio:.0%}",
            "entry_price": round(current_price, 2),
            "stop_loss_price": round(support * 0.995, 2),
            "exit_price": round(resistance, 2) if resistance else None,
            "trigger_reason": (
                f"上升趋势回踩支撑{support:.2f}；量比{volume_ratio:.2f}；"
                f"支撑失效{support * 0.995:.2f}以下不接"
            ),
        }

    if resistance and resistance * 0.98 <= current_price <= resistance * 1.02:
        full_exit = volume_ratio >= 1.5 and gain_20d >= 0.30
        ratio = 1.0 if full_exit else 0.5
        signal_type = "反T-全仓高抛" if full_exit else "反T-半仓高抛"
        return {
            **base,
            "signal_type": signal_type,
            "direction": "反T",
            "price_range": [round(current_price, 2), round(resistance, 2)],
            "t0_shares": _t0_shares(holding_shares, ratio),
            "position_ratio": f"{ratio:.0%}",
            "entry_price": round(current_price, 2),
            "stop_loss_price": round(resistance * 1.01, 2),
            "exit_price": round(support, 2) if support else None,
            "trigger_reason": (
                f"触及压力{resistance:.2f}；量比{volume_ratio:.2f}；"
                f"20日涨幅{gain_20d * 100:.1f}%；突破{resistance * 1.01:.2f}则接回纠错"
            ),
        }

    return None
