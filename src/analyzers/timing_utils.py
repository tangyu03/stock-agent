"""Utility functions for timing engine."""
from typing import Dict, Any, Optional, List


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典（override 覆盖 base）"""
    result = dict(base)
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ============================================================
# 【层间接口修复】策略层 → 分档层：Y（主档基准价）随策略定位
# ============================================================
# 原实现：main_tier_price 一律取 MA10（低吸策略的 Y 语义）——
# 蘅东光 9/8 实证：确认追强 Y=498.26(MA10) 距现价 555.25 达 10.3%，
# 罗博特科趋势延续 Y=577.99(MA10) 距现价 656.10 达 13.5%——
# 策略层在讲追强（突破当日跟进）的语言，分档层却给低吸（回踩均线）
# 的价格。RRR 0.96 就是 Y 挂在下档的产物。
# 修复：主买点不再共用“MA10 低吸模板”，事件诞生时记录公式与输入。
CHASE_STRATEGIES = frozenset({"确认追强", "价量突破", "趋势延续"})
DIP_STRATEGIES = frozenset({"恐慌抄底", "套利低吸"})


def _number_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _nested_config(config: Optional[Dict], *path, default=None):
    node = config or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _breakout_day_low(tech_data: Dict) -> Optional[float]:
    explicit = _number_or_none(tech_data.get("breakout_low"))
    if explicit is not None:
        return explicit
    kline = tech_data.get("kline") or []
    if not kline:
        return None
    bar = kline[-1]
    return _number_or_none(bar.get("最低", bar.get("low")))


def _prev_day_low(tech_data: Dict) -> Optional[float]:
    explicit = _number_or_none(tech_data.get("prev_day_low"))
    if explicit is not None:
        return explicit
    kline = tech_data.get("kline") or []
    if len(kline) < 2:
        return None
    bar = kline[-2]
    return _number_or_none(bar.get("最低", bar.get("low")))


def entry_buypoint(
    entry_type: str,
    trigger_price: float,
    tech_data: Dict,
    config: Optional[Dict] = None,
) -> Tuple[float, str, Dict[str, Any]]:
    """计算策略专属买点 Y，并返回公式版本与可审计输入。"""
    trigger = _number_or_none(trigger_price) or 0.0
    ma5 = _number_or_none(tech_data.get("ma5"))
    ma10 = _number_or_none(tech_data.get("ma10"))

    if entry_type == "确认追强":
        if trigger <= 0:
            return 0.0, "trigger_price", {"trigger_price": 0.0}
        pullback_pct = float(
            _nested_config(config, "tiering", "chase_pullback_pct", default=0.02)
        )
        return (
            round(trigger * (1.0 - pullback_pct), 2),
            "trigger_price*(1-chase_pullback_pct)",
            {
                "trigger_price": round(trigger, 2),
                "chase_pullback_pct": round(pullback_pct, 4),
            },
        )

    if entry_type == "价量突破":
        low = _breakout_day_low(tech_data)
        if low is not None and 0 < low <= trigger:
            return round(low, 2), "breakout_day_low", {"breakout_day_low": round(low, 2)}
        return round(trigger, 2), "trigger_price", {
            "trigger_price": round(trigger, 2),
            "fallback": "breakout_day_low_missing",
        }

    if entry_type == "趋势延续":
        if ma5 is not None and ma5 > 0:
            prev_low = _prev_day_low(tech_data)
            if prev_low is not None and prev_low > 0:
                return (
                    round(max(float(ma5), prev_low), 2),
                    "max(ma5,prev_day_low)",
                    {
                        "ma5": round(float(ma5), 2),
                        "prev_day_low": round(prev_low, 2),
                    },
                )
            return round(float(ma5), 2), "ma5", {"ma5": round(float(ma5), 2)}
        if trigger > 0:
            return round(trigger * 0.98, 2), "trigger_price*0.98", {
                "trigger_price": round(trigger, 2),
                "fallback": "ma5_missing",
            }

    if entry_type in DIP_STRATEGIES and ma10 is not None and ma10 > 0:
        return round(float(ma10), 2), "ma10", {"ma10": round(float(ma10), 2)}

    if ma10 is not None and ma10 > 0:
        return round(float(ma10), 2), "ma10", {"ma10": round(float(ma10), 2)}
    return round(trigger, 2), "trigger_price", {"trigger_price": round(trigger, 2)}


def entry_main_tier_price(
    entry_type: str,
    trigger_price: float,
    tech_data: Dict,
    config: Optional[Dict] = None,
) -> float:
    """兼容旧调用；需要审计时请用 entry_buypoint 取公式与输入。"""
    return entry_buypoint(entry_type, trigger_price, tech_data, config)[0]


def _index_cumulative_metrics(records: Optional[List[Dict]]) -> Optional[Dict]:
    """从指数K线计算近5日累计跌幅%与连续收阴天数（键兼容中文/英文）。

    Returns None when data insufficient（需 ≥6 根K线）。
    """
    if not records or len(records) < 6:
        return None
    closes = []
    for record in records[-6:]:
        value = record.get("close", record.get("收盘", None))
        if value is None:
            return None
        try:
            closes.append(float(value))
        except (TypeError, ValueError):
            return None
    if any(close <= 0 for close in closes) or closes[-6] <= 0:
        return None
    cum_drop = (closes[-1] / closes[-6] - 1.0) * 100.0  # 近5个交易日累计
    down_days = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            down_days += 1
        else:
            break
    return {"cum_drop_5d": round(cum_drop, 2), "down_days_5d": down_days}


def _volume_pattern_not_distribution(tech_data) -> bool:
    '''量价分型是否不判出逃：放量下跌分歧型（外盘占优，承接/对倒不确定）
    与资金接货型/抛压衰竭型属恐慌出清或分歧，不得按放量阴线-派发一刀切冻结。
    修复模块间打架：量价判读免责与事件状态冻结互相矛盾。'''
    try:
        from .volume_pattern import build_volume_pattern
        vp = build_volume_pattern(tech_data or {})
        pattern = vp.get("pattern") or ""
        return pattern in ("放量下跌分歧型", "资金接货型", "抛压衰竭型")
    except Exception:
        return False


