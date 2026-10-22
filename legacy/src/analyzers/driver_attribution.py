"""
第八问驱动源归因（决策记录 P3-2）— 系统不能只看到价格表象
=====================================================

依据：
  博杰 9/7 真实驱动是业绩(+746.9% 预增)+PCB/3C设备板块联动，
  系统只看到"价格表象"（放量上穿 MA25）。驱动源归因让每条信号
  的"为什么涨"进入记录闭环，也为后续"驱动源与 5 日收益相关性"
  的作废条件判定积累数据。

分类优先级（互斥取最强证据）：
  1. 业绩驱动：净利同比 ≥ 50%（或预告预增）—— 博杰类；
  2. 板块联动：板块为主线（主线 = 联动度，同板块共涨）+ 当日涨幅
     与板块共振 —— 中际旭创/蘅东光类；
  3. 资金驱动：量比 ≥ 1.5 + 外盘占优（主动买盘）；
  4. 价格驱动（诚实兜底）：无基本面/板块/资金显著证据 ——
     系统明说"只看到价格表象"，不编造叙事。

作废条件：若驱动源分类与 5 日收益无显著相关，删除该问避免噪声
（每条信号记录驱动源标签，统计函数在 strategy_stats 提供）。
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DRIVER_LABELS = {
    "earnings": "业绩驱动",
    "sector": "板块联动",
    "funds": "资金驱动",
    "price": "价格驱动",
}


def _num(value):
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def classify_driver(
    tech_data: Dict,
    sector_status: str = "",
    sector_name: str = "",
    config: Optional[Dict] = None,
) -> Dict:
    """归因当日驱动源。

    Args:
        tech_data: 择时引擎技术数据（fundamental / volume_ratio /
                   outer_volume / inner_volume / change_pct）
        sector_status: main_trend / rotational / retreating / unknown
        sector_name: 板块展示名

    Returns:
        {driver, label, evidence, dual} —— dual 为复合驱动（业绩+板块）
        时的双标签文案。
    """
    cfg = {
        "earnings_yoy_min": 50.0,
        "funds_volume_ratio_min": 1.5,
    }
    if config and isinstance(config.get("driver_attribution"), dict):
        for key, default in list(cfg.items()):
            value = config["driver_attribution"].get(key)
            if isinstance(value, (int, float)):
                cfg[key] = float(value)

    fundamental = tech_data.get("fundamental") or {}
    profit_yoy = _num(fundamental.get("profit_yoy"))
    forecast = str(fundamental.get("forecast_type") or "")
    volume_ratio = _num(tech_data.get("volume_ratio"))
    outer = _num(tech_data.get("outer_volume"))
    inner = _num(tech_data.get("inner_volume"))

    evidence_parts = []

    # ① 业绩驱动
    earnings_ok = (
        (profit_yoy is not None and profit_yoy >= cfg["earnings_yoy_min"])
        or forecast in ("预增", "略增", "扭亏")
    )
    if earnings_ok:
        if profit_yoy is not None:
            evidence_parts.append(f"净利同比{profit_yoy:+.1f}%")
        if forecast:
            evidence_parts.append(f"预告{forecast}")

    # ② 板块联动
    sector_ok = sector_status == "main_trend"
    if sector_ok:
        evidence_parts.append(f"{sector_name or '主线'}板块(主线联动)")

    # ③ 资金驱动
    funds_ok = bool(
        volume_ratio is not None and volume_ratio >= cfg["funds_volume_ratio_min"]
        and (outer is None or inner is None or outer > inner)
    )
    if funds_ok:
        if volume_ratio is not None:
            evidence_parts.append(f"量比{volume_ratio:.1f}×")
        if outer is not None and inner is not None:
            evidence_parts.append("外盘占优(主动买盘)")

    # 互斥取最强证据：业绩 > 板块 > 资金 > 价格
    if earnings_ok and sector_ok:
        driver = "earnings"
        dual = "业绩驱动+板块联动"
    elif earnings_ok:
        driver = "earnings"
        dual = ""
    elif sector_ok:
        driver = "sector"
        dual = ""
    elif funds_ok:
        driver = "funds"
        dual = ""
    else:
        driver = "price"
        dual = ""

    label = DRIVER_LABELS.get(driver, "价格驱动")
    if driver == "price":
        evidence = "无基本面/板块/资金显著证据——系统只看到价格表象，不编造叙事"
    else:
        evidence = "；".join(evidence_parts) or label

    return {
        "driver": driver,
        "label": label,
        "dual": dual,
        "evidence": evidence,
        "sector_status": sector_status,
        "sector_name": sector_name,
    }


def driver_line(driver_info: Optional[Dict]) -> str:
    """渲染 ⑧驱动源 行（买入卡/观察卡）。"""
    if not driver_info:
        return ""
    label = driver_info.get("dual") or driver_info.get("label") or ""
    evidence = driver_info.get("evidence") or ""
    if not label:
        return ""
    return f"{label}（{evidence}）" if evidence else label
