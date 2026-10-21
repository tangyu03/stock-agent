"""Stock-level shadow point-table generation.

This layer is deliberately independent of sector state and technical voting.
It only materializes each stock's own MA5/support/resistance zones.  Trading
decisions consume these zones separately in ``plan.point_decision``.
"""
import logging
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from ..config_models import load_config
from ..data_layer.stock_data import detect_support_resistance
from ..db import get_connection, init_db
from ..feedback.trade_logger import get_trade_logger
from ..utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)

RULE_VERSION = "shadow_v1"
GENERATED_BY = "shadow_v1"
MAX_UNIVERSE = 30


def next_trading_day(target: date) -> str:
    current = target + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(current):
            return current.strftime("%Y-%m-%d")
        current += timedelta(days=1)
    return current.strftime("%Y-%m-%d")


def calculate_tomorrow_ma_zone(
    closes: Sequence[float], period: int, scenario_ratio: float = 0.02
) -> Optional[Dict[str, float]]:
    """Calculate tomorrow's MA using low/flat/high close scenarios."""
    if period <= 1 or len(closes) < period:
        return None
    base_sum = sum(float(value) for value in closes[-(period - 1):])
    last_close = float(closes[-1])
    if last_close <= 0:
        return None
    estimates = [
        base_sum + last_close * (1 - scenario_ratio),
        base_sum + last_close,
        base_sum + last_close * (1 + scenario_ratio),
    ]
    values = [estimate / period for estimate in estimates]
    return {"low": min(values), "high": max(values), "mid": sum(values) / 3}


def _level_zone(value: float, ratio: float = 0.005) -> Dict[str, float]:
    return {"low": value * (1 - ratio), "high": value * (1 + ratio), "mid": value}


def _market_mode_for(trade_date: str) -> str:
    try:
        with get_connection() as conn:
            row = conn.execute(
                """SELECT mode FROM market_score_history
                   WHERE date <= ? ORDER BY date DESC LIMIT 1""",
                (trade_date,),
            ).fetchone()
            return str((row or {}).get("mode") or "defend")
    except Exception as exc:
        logger.warning("读取市场模式失败，使用 defend: %s", exc)
        return "defend"


def _universe() -> List[Dict[str, str]]:
    portfolio = load_config("portfolio.yaml")
    rows = []
    seen = set()
    for row in portfolio.get("stocks") or []:
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": str(row.get("name") or code)})
        if len(rows) >= MAX_UNIVERSE:
            break
    return rows


def _entry_id(plan_date: str, trade_date: str, code: str, source: str) -> str:
    raw = f"{plan_date}|{trade_date}|{code}|{source}|{RULE_VERSION}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _has_near_entry(entries: Sequence[Dict], zone: Dict[str, float], tolerance: float = 0.005) -> bool:
    """Avoid stacking a BUY and STOP row on effectively the same price zone."""
    center = (float(zone["low"]) + float(zone["high"])) / 2
    for row in entries:
        row_center = (
            float(row.get("zone_low") or 0) + float(row.get("zone_high") or 0)
        ) / 2
        if row_center > 0 and abs(center - row_center) / row_center <= tolerance:
            return True
    return False


def _add_entry(
    entries: List[Dict],
    plan_date: str,
    trade_date: str,
    code: str,
    name: str,
    held: bool,
    source: str,
    zone: Optional[Dict[str, float]],
    action: str,
    action_size: float,
    invalid_if: str,
    verify_volume: str = "NONE",
    verify_time: str = "not_after:14:30",
    scenario: Optional[Dict[str, object]] = None,
) -> None:
    if not zone or zone.get("low", 0) <= 0 or zone.get("high", 0) <= zone.get("low", 0):
        return
    entries.append({
        "id": _entry_id(plan_date, trade_date, code, source),
        "plan_date": plan_date,
        "trade_date": trade_date,
        "symbol": code,
        "stock_name": name,
        "mode": "POINT",
        "zone_low": round(float(zone["low"]), 4),
        "zone_high": round(float(zone["high"]), 4),
        "action": action,
        "action_size": float(action_size),
        "verify_volume": "ANY",
        "verify_market_mode": "ANY",
        "verify_emotion": "NONE",
        "verify_time": verify_time,
        "reverse_action": "SELL" if action == "BUY" else "BUY",
        "invalid_if": invalid_if,
        "source": source,
        "status": "DRAFT",
        "locked": 0,
        "human_signed": 0,
        "market_mode": _market_mode_for(trade_date),
        "generated_by": GENERATED_BY,
        "sector_name": (scenario or {}).get("sector_name", ""),
        "sector_state": (scenario or {}).get("sector_state", ""),
        "sector_rank": (scenario or {}).get("sector_rank", 0),
        "sector_percentile": (scenario or {}).get("sector_percentile", 0),
        "rotation_direction": (scenario or {}).get("rotation_direction", ""),
        "stock_role": (scenario or {}).get("stock_role", ""),
        "scenario_id": (scenario or {}).get("scenario_id", ""),
    })


def _nearest_level(levels: Iterable[float], current: float, below: bool) -> Optional[float]:
    values = [float(value) for value in levels if value > 0]
    candidates = [value for value in values if value < current] if below else [
        value for value in values if value > current
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs(value - current))


def generate_shadow_point_table(
    plan_date: Optional[str] = None,
    codes: Optional[Sequence[str]] = None,
) -> List[Dict]:
    """Generate and persist tomorrow's shadow point rows."""
    init_db()
    generated_date = plan_date or date.today().strftime("%Y-%m-%d")
    effective_date = next_trading_day(
        datetime.strptime(generated_date, "%Y-%m-%d").date()
    )

    universe = _universe()
    if codes is not None:
        wanted = {str(code) for code in codes}
        universe = [row for row in universe if row["code"] in wanted]
    if not universe:
        return []

    try:
        holdings = get_trade_logger().get_current_holdings() or []
        held_codes = {str(row.get("code") or "") for row in holdings}
    except Exception as exc:
        logger.warning("读取持仓失败，按跟踪池处理: %s", exc)
        held_codes = set()

    entries: List[Dict] = []
    from ..analyzers.timing_engine import get_timing_engine

    market_mode = _market_mode_for(effective_date)
    engine = get_timing_engine()
    engine.reset_caches()
    for stock in universe:
        code = stock["code"]
        name = stock["name"]
        held = code in held_codes
        try:
            tech = engine._fetch_tech_data(code, "defend")
            kline = tech.get("kline") or []
            current = float(tech.get("current_price") or 0)
            if current <= 0 or not kline:
                logger.warning("影子点位跳过 %s: 行情数据不足", code)
                continue
            closes = [float(row.get("收盘", row.get("close", 0)) or 0) for row in kline]
            ma5_zone = calculate_tomorrow_ma_zone(closes, 5)
            if ma5_zone:
                action = "BUY" if current > ma5_zone["mid"] else "SELL"
                _add_entry(
                    entries, generated_date, effective_date, code, name, held,
                    "MA5_TOMORROW", ma5_zone,
                    action, 1.0,
                    "BUY:跌破区间转SELL；SELL:站回区间转BUY",
                )

            stop_calc = engine.calculate_stop_loss(code, tech)
            for level in stop_calc.ladder or []:
                support_value = float(level.get("support") or 0)
                if support_value <= 0:
                    continue
                zone = _level_zone(support_value)
                if _has_near_entry(entries, zone):
                    continue
                _add_entry(
                    entries, generated_date, effective_date, code, name, held,
                    f"LADDER_STOP_L{int(level.get('level') or 0)}", zone,
                    "SELL", float(level.get("reduce_ratio") or 0.3),
                    "站回区间上一档转WATCH",
                )

            levels = detect_support_resistance(kline, window=20)
            for index, support in enumerate(levels.get("supports") or [], start=1):
                zone = _level_zone(float(support))
                if _has_near_entry(entries, zone):
                    continue
                _add_entry(
                    entries, generated_date, effective_date, code, name, held,
                    f"PRIOR_LOW_L{index}", zone,
                    "BUY", 0.5,
                    "跌破区间转SELL",
                )

            for index, resistance in enumerate(levels.get("resistances") or [], start=1):
                zone = _level_zone(float(resistance))
                if _has_near_entry(entries, zone):
                    continue
                _add_entry(
                    entries, generated_date, effective_date, code, name, held,
                    f"RESISTANCE_L{index}", zone,
                    "SELL", 0.5,
                    "突破区间转BUY",
                )

        except Exception as exc:
            logger.warning("影子点位生成失败 %s: %s", code, exc)

    _replace_plan(generated_date, effective_date, entries)
    logger.info("影子点位表生成: plan=%s trade=%s entries=%d", generated_date, effective_date, len(entries))
    return entries


def _replace_plan(plan_date: str, trade_date: str, entries: List[Dict]) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM point_entry WHERE plan_date=? AND generated_by=?",
            (plan_date, GENERATED_BY),
        )
        conn.executemany(
            """INSERT INTO point_entry (
                   id, plan_date, trade_date, symbol, stock_name, mode,
                   zone_low, zone_high, action, action_size, verify_volume,
                   verify_market_mode, verify_emotion, verify_time,
                   reverse_action, invalid_if, source, status, locked,
                   human_signed, market_mode, generated_by, sector_name,
                   sector_state, sector_rank, sector_percentile,
                   rotation_direction, stock_role, scenario_id, created_at
               ) VALUES (
                   :id, :plan_date, :trade_date, :symbol, :stock_name, :mode,
                   :zone_low, :zone_high, :action, :action_size, :verify_volume,
                   :verify_market_mode, :verify_emotion, :verify_time,
                   :reverse_action, :invalid_if, :source, :status, :locked,
                   :human_signed, :market_mode, :generated_by, :sector_name,
                   :sector_state, :sector_rank, :sector_percentile,
                   :rotation_direction, :stock_role, :scenario_id, CURRENT_TIMESTAMP
               )""",
            entries,
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def list_shadow_point_table(plan_date: str) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM point_entry
               WHERE plan_date=? AND generated_by=?
               ORDER BY symbol, zone_low""",
            (plan_date, GENERATED_BY),
        ).fetchall()
    return [dict(row) for row in rows]


def render_shadow_point_table(entries: Sequence[Dict]) -> str:
    if not entries:
        return "  暂无数据"

    by_symbol: Dict[str, List[Dict]] = {}
    for entry in entries:
        by_symbol.setdefault(str(entry.get("symbol")), []).append(entry)

    trade_date = str(entries[0].get("trade_date") or "")
    lines = [
        f"  生效日: {trade_date}｜状态: 影子运行，未签字、不触发、不参与调度",
        "  点位｜动作｜反向｜失效",
    ]
    action_names = {
        "BUY": "买点",
        "SELL": "卖点",
    }
    source_names = {
        "MA5_TOMORROW": "明日MA5",
        "PRIOR_LOW": "前期低点",
        "RESISTANCE": "前期高点",
    }
    for code, rows in by_symbol.items():
        name = str(rows[0].get("stock_name") or code)
        lines.append(f"  {name}({code})")
        for row in rows:
            action = action_names.get(str(row.get("action")), str(row.get("action")))
            source = source_names.get(str(row.get("source")), str(row.get("source")))
            lines.append(
                f"    {source} {float(row.get('zone_low')):.2f}-{float(row.get('zone_high')):.2f}｜"
                f"{action} {float(row.get('action_size') or 0):.2f}成｜"
                f"{row.get('verify_volume')}｜{row.get('reverse_action')}｜{row.get('invalid_if')}"
            )
    return "\n".join(lines)
