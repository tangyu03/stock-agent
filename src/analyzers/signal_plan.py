"""Single-source execution planning for entry signals."""

from datetime import date
from pathlib import Path

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VolumeSnapshot:
    volume_ratio: Optional[float] = None
    volume_vs_ma60: Optional[float] = None
    turnover_rate: Optional[float] = None
    turnover_p25: Optional[float] = None
    turnover_p50: Optional[float] = None
    turnover_p75: Optional[float] = None
    turnover_p90: Optional[float] = None
    volume_ratio_p25: Optional[float] = None
    volume_ratio_p50: Optional[float] = None
    volume_ratio_p75: Optional[float] = None
    volume_ratio_p90: Optional[float] = None
    sample_count: int = 0
    turnover_hot: bool = False
    volume_hot: bool = False
    shrinking: bool = False
    label: str = "数据不足"
    data_ok: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FundSnapshot:
    main_flows: List[float] = field(default_factory=list)
    main_total: Optional[float] = None
    main_vote: int = 0
    main_strong: bool = False
    super_large_flows: List[float] = field(default_factory=list)
    large_flows: List[float] = field(default_factory=list)
    latest_super_large_net: Optional[float] = None
    latest_large_net: Optional[float] = None
    order_confirmation: str = "数据不足"
    disagreement: bool = False
    vote: int = 0
    institutional_score: int = 0
    institutional_adjustment: int = 0
    shareholder_change_pct: Optional[float] = None
    top10_institutional_change_points: Optional[float] = None
    institutional_shareholder_divergence: bool = False
    suspected_distribution: bool = False
    machine_tags: List[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExecutionPlan:
    entry_type: str
    benchmark_price: float
    stop_loss: float
    target_range: List[float]
    risk_pct: Optional[float] = None
    reward_low_pct: Optional[float] = None
    reward_high_pct: Optional[float] = None
    rrr_low: Optional[float] = None
    rrr_high: Optional[float] = None
    confidence_score: float = 0
    applicable_score: int = 0
    confidence: str = "低"
    confidence_details: List[str] = field(default_factory=list)
    volume_snapshot: VolumeSnapshot = field(default_factory=VolumeSnapshot)
    fund_snapshot: FundSnapshot = field(default_factory=FundSnapshot)
    risk_multipliers: Dict[str, float] = field(default_factory=dict)
    combined_risk_multiplier: float = 1.0
    industry_multiplier: float = 1.0
    industry_tags: List[str] = field(default_factory=list)
    execution_tiers: List[Dict[str, Any]] = field(default_factory=list)
    hard_constraint_notes: List[str] = field(default_factory=list)
    execute: bool = False

    def as_dict(self) -> Dict[str, Any]:
        result = self.__dict__.copy()
        result["volume_snapshot"] = self.volume_snapshot.as_dict()
        result["fund_snapshot"] = self.fund_snapshot.as_dict()
        return result


def _percentile(values: List[float], ratio: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def build_volume_snapshot(tech_data: Dict[str, Any], min_samples: int = 60) -> VolumeSnapshot:
    kline = tech_data.get("kline") or []
    volumes = [
        _number(item.get("volume", item.get("成交量", 0))) or 0.0
        for item in kline
    ]
    today_volume = _number(tech_data.get("today_volume"))
    if today_volume is None and volumes:
        today_volume = volumes[-1]

    volume_ratio = _number(tech_data.get("volume_ratio"))
    volume_ma60 = _number(tech_data.get("volume_ma60"))
    if volume_ma60 is None and len(volumes) >= 60:
        volume_ma60 = sum(volumes[-60:]) / 60
    volume_vs_ma60 = (
        today_volume / volume_ma60
        if today_volume and volume_ma60 and volume_ma60 > 0
        else None
    )

    # Historical ratios exclude the current bar so intraday volume cannot move its own threshold.
    volume_ratios: List[float] = []
    for index in range(5, len(volumes) - 1):
        base = sum(volumes[index - 5:index]) / 5
        if base > 0:
            volume_ratios.append(volumes[index] / base)
    volume_ratios = volume_ratios[-min_samples:]

    turnover_values: List[float] = []
    for item in kline:
        value = _number(item.get("turnover_rate", item.get("换手率")))
        if value is not None:
            turnover_values.append(value)
    today_turnover = _number(tech_data.get("turnover_rate"))
    if today_turnover is None and turnover_values:
        today_turnover = turnover_values[-1]
    turnover_history = turnover_values[-(min_samples + 1):-1] if len(turnover_values) > min_samples else []

    snapshot = VolumeSnapshot(
        volume_ratio=volume_ratio,
        volume_vs_ma60=volume_vs_ma60,
        turnover_rate=today_turnover,
        turnover_p25=_percentile(turnover_history, 0.25),
        turnover_p50=_percentile(turnover_history, 0.50),
        turnover_p75=_percentile(turnover_history, 0.75),
        turnover_p90=_percentile(turnover_history, 0.90),
        volume_ratio_p25=_percentile(volume_ratios, 0.25),
        volume_ratio_p50=_percentile(volume_ratios, 0.50),
        volume_ratio_p75=_percentile(volume_ratios, 0.75),
        volume_ratio_p90=_percentile(volume_ratios, 0.90),
        sample_count=min(len(turnover_history), len(volume_ratios)),
    )

    has_turnover = (
        snapshot.turnover_rate is not None
        and snapshot.turnover_p25 is not None
        and snapshot.turnover_p90 is not None
    )
    has_volume = (
        snapshot.volume_ratio is not None
        and snapshot.volume_ratio_p25 is not None
        and snapshot.volume_ratio_p90 is not None
    )
    snapshot.data_ok = has_turnover or has_volume
    snapshot.turnover_hot = bool(
        has_turnover
        and snapshot.turnover_rate is not None
        and snapshot.turnover_rate > (snapshot.turnover_p90 or 0)
    )
    snapshot.volume_hot = bool(
        has_volume
        and snapshot.volume_ratio is not None
        and snapshot.volume_ratio > (snapshot.volume_ratio_p90 or 0)
    )
    snapshot.shrinking = bool(
        has_volume
        and snapshot.volume_ratio is not None
        and snapshot.volume_ratio < (snapshot.volume_ratio_p25 or 0)
    )
    if snapshot.turnover_hot:
        snapshot.label = "换手过热"
    elif snapshot.volume_hot:
        snapshot.label = "量比过热"
    elif snapshot.shrinking:
        snapshot.label = "缩量"
    elif snapshot.data_ok:
        snapshot.label = "正常"
    return snapshot


def _latest(series: List[Any]) -> Optional[float]:
    for value in reversed(series):
        number = _number(value)
        if number is not None:
            return number
    return None


def _is_near_5d_stagnant(tech_data: Dict[str, Any]) -> bool:
    closes = [
        _number(item.get("close", item.get("收盘", item.get("收盘价"))))
        for item in (tech_data.get("kline") or [])
    ]
    closes = [value for value in closes if value is not None]
    if len(closes) < 5:
        return False
    base = closes[-6] if len(closes) >= 6 else closes[-5]
    if base <= 0:
        return False
    return abs(closes[-1] / base - 1.0) < 0.03


def _industry_tuning(
    sector_name: str,
    config_path: Optional[Path] = None,
) -> tuple[float, List[str]]:
    """Read the declarative industry calendar; missing config is neutral."""
    if not sector_name:
        return 1.0, []
    config_path = config_path or (
        Path(__file__).resolve().parents[2] / "config" / "industry_tuning.yaml"
    )
    if not config_path.exists():
        return 1.0, []

    try:
        import yaml

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return 1.0, []

    multiplier_by_status = {
        "disclosed": 1.2,
        "已披露": 1.2,
        "forecast": 1.0,
        "机构预测": 1.0,
        "unverified": 0.8,
        "待验证": 0.8,
    }
    multipliers: List[float] = []
    tags: List[str] = []
    today = date.today()
    for event in config.get("industry_events") or []:
        if not isinstance(event, dict):
            continue
        sectors = event.get("sector") or event.get("sectors") or []
        if isinstance(sectors, str):
            sectors = [sectors]
        if sector_name not in sectors:
            continue

        expires = str(event.get("expires") or "")
        if expires:
            try:
                if today > date.fromisoformat(expires):
                    status = "unverified"
                else:
                    status = str(event.get("status", "forecast")).lower()
            except ValueError:
                status = str(event.get("status", "forecast")).lower()
        else:
            status = str(event.get("status", "forecast")).lower()
        multiplier = multiplier_by_status.get(status, 1.0)
        multipliers.append(multiplier)
        note = str(event.get("note", "")).strip()
        tags.append(f"{sector_name}:{status}{'' if not note else '·' + note}")

    if not multipliers:
        return 1.0, []
    return min(multipliers), tags


def _execution_tier(
    name: str,
    role: str,
    price: float,
    current_price: Optional[float],
    action: str,
) -> Dict[str, Any]:
    state = "数据不足"
    trigger_text = action
    distance = ""
    if current_price is not None:
        distance_pct = abs(current_price / price - 1.0) * 100
        if current_price >= price:
            state = "上方"
            distance = f"上方{distance_pct:.1f}%"
            trigger_text = f"回踩不破{price:.2f}"
        else:
            state = "已触发" if role == "stop" else "已下破"
            distance = f"下方{distance_pct:.1f}%"
            trigger_text = f"反弹收复{price:.2f}"
    return {
        "name": name,
        "role": role,
        "price": price,
        "state": state,
        "distance": distance,
        "trigger": trigger_text,
    }


def _build_execution_tiers(
    benchmark_price: float,
    stop_loss: float,
    tech_data: Dict[str, Any],
    volume: VolumeSnapshot,
    fund: FundSnapshot,
) -> List[Dict[str, Any]]:
    current = _number(tech_data.get("current_price"))
    ma5 = _number(tech_data.get("ma5"))
    ma10 = _number(tech_data.get("ma10"))
    tiers: List[Dict[str, Any]] = []
    if ma10 and ma10 > 0:
        tiers.append(_execution_tier("MA10档", "main", ma10, current, "回踩确认"))
    elif benchmark_price > 0:
        tiers.append(_execution_tier("主档", "main", benchmark_price, current, "回踩确认"))
    if ma5 and ma5 > 0:
        tiers.append(_execution_tier("MA5档", "probe", ma5, current, "缩量试探"))
    if stop_loss > 0:
        tier = _execution_tier("止损", "stop", stop_loss, current, "放量跌破离场")
        if current is not None and current <= stop_loss:
            tier["state"] = "已触发"
            tier["trigger"] = "放量跌破离场,禁止补仓"
        tiers.append(tier)
    for tier in tiers:
        if tier["role"] == "stop":
            continue
        if volume.shrinking:
            volume_condition = "量比<P25"
        elif (
            volume.volume_ratio is not None
            and volume.volume_ratio_p25 is not None
            and volume.volume_ratio_p75 is not None
            and volume.volume_ratio_p25 <= volume.volume_ratio <= volume.volume_ratio_p75
        ):
            volume_condition = "量比P25-P75"
        else:
            volume_condition = "量比分位不足"
        if (
            fund.latest_super_large_net is not None
            and fund.latest_large_net is not None
            and fund.latest_super_large_net > 0
            and fund.latest_large_net > 0
        ):
            order_condition = "大单同向流入"
        elif fund.disagreement:
            order_condition = "大单分歧"
        else:
            order_condition = "大单结构未确认"
        tier["conditions"] = f"{volume_condition}+{order_condition}"
    return tiers


def build_fund_snapshot(
    institutional: Optional[Dict[str, Any]],
    tech_data: Optional[Dict[str, Any]] = None,
) -> FundSnapshot:
    votes = (institutional or {}).get("votes") or {}
    main = votes.get("main_force") or {}
    raw = main.get("raw") or {}
    flows = raw.get("net_flows_5d") or raw.get("net_flows") or []
    super_flows = raw.get("super_large_flows_5d") or []
    large_flows = raw.get("large_flows_5d") or []
    latest_super = _latest(super_flows) if super_flows else raw.get("latest_super_large_net")
    latest_large = _latest(large_flows) if large_flows else raw.get("latest_large_net")
    main_flows = [_number(value) for value in flows if _number(value) is not None]

    main_vote = int(main.get("vote", 0) or 0)
    main_strong = bool(raw.get("strong"))
    if main.get("vote") and "strong" not in raw and len(main_flows) >= 3:
        main_strong = (
            all(value > 0 for value in main_flows[-3:])
            or all(value < 0 for value in main_flows[-3:])
        )
    disagreement = bool(
        latest_super is not None
        and latest_large is not None
        and latest_super * latest_large < 0
    )
    if latest_super is None or latest_large is None:
        order_confirmation = "数据不足"
    elif disagreement:
        order_confirmation = "大资金分歧"
    else:
        order_confirmation = "同向"

    vote = 0 if disagreement else main_vote
    shareholder_raw = (votes.get("shareholder") or {}).get("raw") or {}
    shareholder_change = _number(shareholder_raw.get("change_pct"))
    top10 = (institutional or {}).get("top10_institutional_ratio") or {}
    top10_change = _number(top10.get("change_points"))

    machine_tags: List[str] = []
    if disagreement:
        machine_tags.append("大资金分歧")
    if (
        top10_change is not None
        and top10_change != 0
        and shareholder_change is not None
        and shareholder_change != 0
        and top10_change * shareholder_change < 0
    ):
        machine_tags.append("机构散户分歧")

    main_total = _number(raw.get("total"))
    if main_total is None:
        main_total = sum(value for value in main_flows if value is not None) if flows else None

    suspected_distribution = bool(
        main_total is not None
        and main_total > 0
        and shareholder_change is not None
        and shareholder_change > 0.20
        and _is_near_5d_stagnant(tech_data or {})
    )
    institutional_adjustment = -1 if suspected_distribution else 0
    if suspected_distribution:
        machine_tags.append("疑似派发")

    return FundSnapshot(
        main_flows=main_flows,
        main_total=main_total,
        main_vote=main_vote,
        main_strong=main_strong,
        super_large_flows=[_number(value) for value in super_flows if _number(value) is not None],
        large_flows=[_number(value) for value in large_flows if _number(value) is not None],
        latest_super_large_net=latest_super,
        latest_large_net=latest_large,
        order_confirmation=order_confirmation,
        disagreement=disagreement,
        vote=vote,
        institutional_score=int((institutional or {}).get("vote_score", 0) or 0),
        institutional_adjustment=institutional_adjustment,
        shareholder_change_pct=shareholder_change,
        top10_institutional_change_points=top10_change,
        institutional_shareholder_divergence="机构散户分歧" in machine_tags,
        suspected_distribution=suspected_distribution,
        machine_tags=machine_tags,
        source=str(raw.get("source") or "问财逐日序列"),
    )


def _net_technical_votes(tech_data: Dict[str, Any]) -> Optional[float]:
    tech = tech_data.get("tech_signals") or {}
    if tech.get("vote_score") is not None:
        return _number(tech.get("vote_score"))
    categories = tech.get("category_votes") or {}
    if not categories:
        return None
    return sum(
        int(item.get("vote", 0) or 0) * float(item.get("weight", 1) or 1)
        for item in categories.values()
    )


def _signal_conflicts(tech_data: Dict[str, Any], direction: int = 1) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    tech = tech_data.get("tech_signals") or {}
    ma5 = _number(tech_data.get("ma5"))
    ma10 = _number(tech_data.get("ma10"))
    ma20 = _number(tech_data.get("ma20"))
    if ma5 and ma10 and ma20:
        bullish = ma5 > ma10 > ma20
        bearish = ma5 < ma10 < ma20
        if direction > 0 and bearish:
            conflicts.append({"label": "MA空头排列", "severity": 2})
        elif direction < 0 and bullish:
            conflicts.append({"label": "MA多头排列", "severity": 2})

    pattern_vote = int(((tech.get("category_votes") or {}).get("pattern") or {}).get("vote", 0) or 0)
    if direction > 0 and pattern_vote < 0:
        conflicts.append({"label": "K线偏空", "severity": 1})
    elif direction < 0 and pattern_vote > 0:
        conflicts.append({"label": "K线偏多", "severity": 1})

    rsi = _number(tech_data.get("rsi") or tech.get("rsi"))
    if rsi is not None:
        if direction > 0 and rsi >= 65:
            conflicts.append({"label": "RSI过热", "severity": 1})
        elif direction < 0 and rsi <= 35:
            conflicts.append({"label": "RSI超卖", "severity": 1})

    divergence = tech.get("chan_divergence") or {}
    divergence_type = divergence.get("type")
    if divergence_type == "顶背驰" and direction > 0:
        conflicts.append({
            "label": "MACD顶背驰"
                     + (f"({divergence.get('confidence')})" if divergence.get("confidence") else ""),
            "severity": 2 if divergence.get("confidence") == "高" else 1,
        })
    elif divergence_type == "底背驰" and direction < 0:
        conflicts.append({
            "label": "MACD底背驰"
                     + (f"({divergence.get('confidence')})" if divergence.get("confidence") else ""),
            "severity": 2 if divergence.get("confidence") == "高" else 1,
        })
    return conflicts


def _confidence_label(score: int) -> str:
    if score >= 4:
        return "高"
    if score >= 2:
        return "中"
    return "低"


def build_execution_plan(
    entry_type: str,
    benchmark_price: float,
    stop_loss: float,
    target_range: List[float],
    tech_data: Dict[str, Any],
    sector_status: str = "",
    sector_name: str = "",
) -> ExecutionPlan:
    benchmark = _number(benchmark_price) or 0.0
    stop = _number(stop_loss) or 0.0
    targets = [_number(value) for value in (target_range or []) if _number(value) is not None]
    risk_abs = benchmark - stop if benchmark > 0 and stop > 0 and benchmark > stop else None
    risk_pct = risk_abs / benchmark if risk_abs is not None else None
    rewards = [(value - benchmark) / benchmark for value in targets if benchmark > 0 and value > benchmark]
    risk_ratio = risk_abs if risk_abs and risk_abs > 0 else None
    rrr_low = (targets[0] - benchmark) / risk_ratio if targets and risk_ratio else None
    rrr_high = (targets[-1] - benchmark) / risk_ratio if len(targets) >= 2 and risk_ratio else rrr_low

    volume = build_volume_snapshot(tech_data)
    fund = build_fund_snapshot(tech_data.get("institutional_holding"), tech_data)
    details: List[str] = []
    score = 0

    technical_votes = _net_technical_votes(tech_data)
    if technical_votes is not None and technical_votes * 1 >= 2:
        score += 1
        details.append("技术同向+1(①②③合成)")
    elif technical_votes is not None:
        details.append(f"技术票{technical_votes:+.1f}(①②③合成)")
    else:
        details.append("技术票缺失")

    institutional_score = fund.institutional_score + fund.institutional_adjustment
    if institutional_score >= 2:
        score += 1
        details.append("机构同向+1")
    else:
        details.append(f"机构票{institutional_score:+d}")

    if sector_status == "main_trend":
        score += 1
        details.append("主线+1")
    else:
        details.append("非主线")

    conflicts = _signal_conflicts(tech_data)
    conflict_labels = [str(item["label"]) for item in conflicts]
    strong_conflicts = [item for item in conflicts if int(item["severity"]) >= 2]
    if not conflicts:
        score += 1
        details.append("无矛盾+1")
    elif strong_conflicts or len(conflicts) >= 2:
        score -= 1
        details.append("矛盾-1:" + "/".join(conflict_labels))
    else:
        details.append("矛盾0:" + "/".join(conflict_labels))

    market_score = _number(tech_data.get("market_score"))
    if market_score is not None:
        if market_score >= 6:
            score += 1
            details.append(f"环境{market_score:.1f}+1")
        else:
            details.append(f"环境{market_score:.1f}")

    tech = tech_data.get("tech_signals") or {}
    adx = _number(tech_data.get("adx") or tech.get("adx"))
    if adx is not None:
        if adx > 25:
            adx_label = "单边力度强,方向需看MACD"
        elif adx < 15:
            adx_label = "价格反复拉锯,突破易失败"
        else:
            adx_label = "单边力度一般,方向不稳定"
        details.append(f"ADX{adx:.1f}({adx_label},不评分)")
    else:
        details.append("ADX缺失")

    score = max(0, score)
    applicable_score = 5 + (1 if market_score is not None else 0)
    confidence = _confidence_label(score)
    if rrr_low is None:
        confidence = "低"
        confidence = _confidence_label(min(confidence_score_value(confidence), 1))
        details.append("RRR缺失")
    elif rrr_low < 2.0 and confidence == "高":
        confidence = "中"
        details.append(f"RRR{rrr_low:.2f}<2禁止高")
    elif rrr_low < 1.5:
        downgraded = {"高": "中", "中": "低", "低": "低"}[confidence]
        details.append(f"RRR{rrr_low:.2f}<1.5降档")
        confidence = downgraded

    multipliers = {"base": 1.0}
    if volume.turnover_hot:
        multipliers["turnover_hot"] = 0.5
    if fund.shareholder_change_pct is not None and fund.shareholder_change_pct > 0.20:
        multipliers["shareholder_increase"] = 0.8
    combined = 1.0
    for multiplier in multipliers.values():
        combined *= multiplier

    industry_multiplier, industry_tags = _industry_tuning(sector_name)
    tiers = _build_execution_tiers(benchmark, stop, tech_data, volume, fund)

    plan = ExecutionPlan(
        entry_type=entry_type,
        benchmark_price=benchmark,
        stop_loss=stop,
        target_range=targets,
        risk_pct=risk_pct,
        reward_low_pct=rewards[0] if rewards else None,
        reward_high_pct=rewards[-1] if rewards else None,
        rrr_low=rrr_low,
        rrr_high=rrr_high,
        confidence_score=score,
        applicable_score=applicable_score,
        confidence=confidence,
        confidence_details=details,
        volume_snapshot=volume,
        fund_snapshot=fund,
        risk_multipliers=multipliers,
        combined_risk_multiplier=combined,
        industry_multiplier=industry_multiplier,
        industry_tags=industry_tags,
        execution_tiers=tiers,
        hard_constraint_notes=[],
        execute=True,
    )
    return plan


def confidence_score_value(label: str) -> int:
    return {"高": 4, "中": 2, "低": 0}.get(label, 0)
