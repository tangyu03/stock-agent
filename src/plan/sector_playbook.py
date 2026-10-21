"""Sector-conditioned playbooks for shadow point entries.

A point is not a standalone signal.  The same MA5/support/resistance level can
mean add, trim or exit depending on the sector regime and rotation direction.
"""
from typing import Dict, Optional


def derive_rotation_direction(
    sector_state: str,
    change_pct: Optional[float],
    rank: int = 0,
    total: int = 0,
) -> str:
    """Classify today's sector direction inside its broader regime."""
    change = float(change_pct or 0)
    if sector_state == "main_trend":
        if change >= 1.0:
            return "rising"
        if change < -0.5:
            return "fading"
        return "holding"
    if sector_state == "rotational":
        if change >= 1.0:
            return "rising"
        if change <= -0.5:
            return "fading"
        return "holding"
    if sector_state == "retreating":
        if change >= 0.5:
            return "stabilizing"
        return "fading"
    return "unknown"


def derive_stock_role(
    stock_change_pct: Optional[float], sector_change_pct: Optional[float]
) -> str:
    stock = float(stock_change_pct or 0)
    sector = float(sector_change_pct or 0)
    spread = stock - sector
    if spread >= 1.0:
        return "leader"
    if spread <= -1.0:
        return "laggard"
    return "in_line"


def build_scenario(
    market_mode: str,
    sector_state: str,
    sector_name: str,
    sector_change_pct: Optional[float],
    sector_rank: int = 0,
    sector_total: int = 0,
    stock_change_pct: Optional[float] = None,
) -> Dict[str, object]:
    state = sector_state or "unknown"
    rotation = derive_rotation_direction(
        state, sector_change_pct, sector_rank, sector_total
    )
    percentile = 0.0
    if sector_rank > 0 and sector_total > 0:
        percentile = round((sector_rank - 1) / sector_total, 4)
    return {
        "market_mode": market_mode or "defend",
        "sector_name": sector_name or "",
        "sector_state": state,
        "sector_rank": int(sector_rank or 0),
        "sector_percentile": percentile,
        "sector_change_pct": float(sector_change_pct or 0),
        "rotation_direction": rotation,
        "stock_role": derive_stock_role(stock_change_pct, sector_change_pct),
        "scenario_id": f"{state}_{rotation}",
    }


def scenario_label(scenario: Dict[str, object]) -> str:
    names = {
        ("main_trend", "rising"): "主线上升",
        ("main_trend", "holding"): "主线震荡",
        ("main_trend", "fading"): "主线走弱",
        ("rotational", "rising"): "轮动启动",
        ("rotational", "holding"): "轮动延续",
        ("rotational", "fading"): "轮动退潮",
        ("retreating", "stabilizing"): "退潮修复观察",
        ("retreating", "fading"): "板块退潮",
    }
    key = (str(scenario.get("sector_state")), str(scenario.get("rotation_direction")))
    return names.get(key, f"{scenario.get('sector_state')}_{scenario.get('rotation_direction')}")


def ma5_rule(scenario: Dict[str, object], above_ma5: bool, held: bool) -> Dict[str, object]:
    key = (str(scenario.get("sector_state")), str(scenario.get("rotation_direction")))
    role = str(scenario.get("stock_role"))

    if key == ("main_trend", "rising"):
        if above_ma5:
            return {
                "action": "ADD" if held else "WATCH",
                "action_size": 0.50 if held else 0.25,
                "verify_volume": "volume_ratio<=1.2",
                "invalid_if": "板块掉出主线上升剧本或放量跌破MA10",
            }
        return {
            "action": "STOP_DELAY" if held else "WATCH",
            "action_size": 0.50,
            "verify_volume": "volume_ratio>=1.0",
            "invalid_if": "放量站回MA5且板块重新走强",
        }

    if key == ("main_trend", "holding"):
        if above_ma5:
            return {
                "action": "ADD" if held else "WATCH",
                "action_size": 0.25,
                "verify_volume": "volume_ratio<=1.0",
                "invalid_if": "板块连续走弱或放量跌破MA10",
            }
        return {
            "action": "WATCH" if held else "WATCH",
            "action_size": 0.25,
            "verify_volume": "volume_ratio>=1.0",
            "invalid_if": "放量跌破MA5",
        }

    if key == ("rotational", "rising"):
        return {
            "action": "WATCH",
            "action_size": 0.25,
            "verify_volume": "volume_ratio>=1.0",
            "invalid_if": "板块转轮动退潮或个股放量破位",
        }

    if key == ("retreating", "fading") or key == ("main_trend", "fading"):
        return {
            "action": "STOP_DELAY" if held else "WATCH",
            "action_size": 0.75 if key == ("retreating", "fading") else 0.50,
            "verify_volume": "ANY",
            "invalid_if": "板块状态恢复为轮动以上且放量收复MA5",
        }

    return {
        "action": "WATCH",
        "action_size": 0.25,
        "verify_volume": "ANY",
        "invalid_if": "个股或板块状态变化",
    }


def support_rule(scenario: Dict[str, object], held: bool) -> Dict[str, object]:
    key = (str(scenario.get("sector_state")), str(scenario.get("rotation_direction")))
    if key == ("main_trend", "rising"):
        return {
            "action": "ADD" if held else "WATCH",
            "action_size": 0.50,
            "verify_volume": "volume_ratio<=1.0且板块不退潮",
            "invalid_if": "板块掉出主线或放量跌破区间",
        }
    if key == ("main_trend", "holding") or key == ("rotational", "rising"):
        return {
            "action": "ADD" if held else "WATCH",
            "action_size": 0.25,
            "verify_volume": "volume_ratio>=1.0且板块方向不转弱",
            "invalid_if": "板块转轮动退潮或放量跌破区间",
        }
    return {
        "action": "WATCH",
        "action_size": 0.25,
        "verify_volume": "ANY",
        "invalid_if": "板块状态不支持低吸",
    }


def resistance_rule(scenario: Dict[str, object], held: bool) -> Dict[str, object]:
    key = (str(scenario.get("sector_state")), str(scenario.get("rotation_direction")))
    if key == ("main_trend", "rising"):
        return {
            "action": "SELL_PART" if held else "WATCH",
            "action_size": 0.25,
            "verify_volume": "volume_ratio>=1.2且出现量价背离",
            "invalid_if": "放量突破且板块继续主线上升",
        }
    if key in {
        ("main_trend", "holding"), ("main_trend", "fading"),
        ("rotational", "holding"), ("rotational", "fading"),
    }:
        return {
            "action": "SELL_PART" if held else "WATCH",
            "action_size": 0.50,
            "verify_volume": "volume_ratio>=1.0",
            "invalid_if": "放量突破且板块方向转rising",
        }
    if key == ("retreating", "fading"):
        return {
            "action": "SELL_PART" if held else "WATCH",
            "action_size": 0.75,
            "verify_volume": "ANY",
            "invalid_if": "板块状态恢复为轮动以上",
        }
    return {
        "action": "WATCH",
        "action_size": 0.25,
        "verify_volume": "ANY",
        "invalid_if": "板块状态未知，不解释压力位",
    }

