"""Single-source execution planning for entry signals."""

from datetime import date
from pathlib import Path

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 【二】数据层一致性守卫默认值（口径统一：全链路成交量统一为“股”）
# 科创板“股被当手”两类拦截：
#   规则1: 量比<1 而 量能倍数>10  → 今日量被放大百倍（股/手错位）
#   规则2: 换手<10% 而成交量>10亿股 → 换手与量级矛盾（同上，反向证据）
# 阈值可由 config/timing.yaml data_guard 覆盖。
# ============================================================
DATA_GUARD_DEFAULTS: Dict[str, float] = {
    "turnover_threshold_pct": 10.0,     # 换手率阈值（%）
    "max_volume_shares": 1.0e9,         # 成交量阈值（股）：10亿股
}


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
    dirty: bool = False
    dirty_reason: str = ""
    label: str = "数据不足"
    data_ok: bool = False
    # 【Phase4 P0-1】外推口径：盘中累计量/60日均量 → 全天量/60日均量
    projected_volume_vs_ma60: Optional[float] = None
    projection_mode: str = ""        # ok / pre_window / limit_locked / non_trading ...
    projection_note: str = ""

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
    # 【一】可证伪假说：X/Y/Z/W 及出厂检查结论
    hypothesis: Dict[str, Any] = field(default_factory=dict)
    hypothesis_rejected: bool = False
    rejection_reasons: List[str] = field(default_factory=list)
    # 【二】基本面闸门：业绩雷 veto / 盈利质量·财报窗口 warn（Phase2-A）
    fundamental: Optional[Dict[str, Any]] = None
    fundamental_rejected: bool = False
    # 【Phase3】估值透镜：估值泡沫/低基数反转/亏损分型/资金-分析师冲突
    valuation: Optional[Dict[str, Any]] = None
    valuation_rejected: bool = False
    # 【评分层→决策层】期望值闸门结论（EV = W×R−(1−W)）：
    # 0/6 低置信不再放行；策略统计足样本时用真实胜率 W 算 EV，负期望拒绝。
    ev_gate: Dict[str, Any] = field(default_factory=dict)

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


def _config_get(config: Optional[Dict], *path, default=None):
    """读取嵌套配置（缺省返回 default）——【P0-2】置信度质量票等处使用。"""
    node = config or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _guard_value(guard: Optional[Dict], key: str) -> float:
    """读守卫阈值：传入 guard 覆盖 > 模块默认。"""
    if guard:
        try:
            value = float(guard.get(key, DATA_GUARD_DEFAULTS[key]))
            return value
        except (TypeError, ValueError, KeyError):
            pass
    return float(DATA_GUARD_DEFAULTS[key])


def build_volume_snapshot(
    tech_data: Dict[str, Any],
    min_samples: int = 60,
    guard: Optional[Dict[str, Any]] = None,
    projection_config: Optional[Dict[str, Any]] = None,
    limit_ratio: Optional[float] = None,
) -> VolumeSnapshot:
    kline = tech_data.get("kline") or []
    volumes = [
        _number(item.get("volume", item.get("成交量", 0))) or 0.0
        for item in kline
    ]
    today_volume = _number(tech_data.get("today_volume"))
    if today_volume is None and volumes:
        today_volume = volumes[-1]

    # 换手率先行（一致性校验需要；原逻辑在后面才算，提前到此处）
    turnover_values: List[float] = []
    for item in kline:
        value = _number(item.get("turnover_rate", item.get("换手率")))
        if value is not None:
            turnover_values.append(value)
    today_turnover = _number(tech_data.get("turnover_rate"))
    if today_turnover is None and turnover_values:
        today_turnover = turnover_values[-1]

    volume_ratio = _number(tech_data.get("volume_ratio"))
    volume_ma60 = _number(tech_data.get("volume_ma60"))
    if volume_ma60 is None and len(volumes) >= 60:
        volume_ma60 = sum(volumes[-60:]) / 60
    volume_vs_ma60 = (
        today_volume / volume_ma60
        if today_volume and volume_ma60 and volume_ma60 > 0
        else None
    )
    # 【二】规则1: 量比<1 而量能倍数>10 → 量能口径冲突（股/手错位放大百倍）
    dirty = bool(
        volume_ratio is not None
        and volume_ratio < 1.0
        and volume_vs_ma60 is not None
        and volume_vs_ma60 > 10.0
    )
    dirty_reason = (
        f"量能口径冲突(量比{volume_ratio:.2f}<1, 60日均量倍数{volume_vs_ma60:.2f}>10)"
        if dirty else ""
    )
    # 【二】规则2: 换手<10% 而成交量>10亿股 → 换手与量级矛盾，同判脏数据
    # （沃尔德 9/4 案例：换手 4.8% 而成交量口径被放大后 >10 亿股，接口层单位错位）
    if not dirty and today_turnover is not None and today_volume is not None:
        turnover_thresh = _guard_value(guard, "turnover_threshold_pct")
        max_volume = _guard_value(guard, "max_volume_shares")
        if today_turnover < turnover_thresh and today_volume > max_volume:
            dirty = True
            dirty_reason = (
                f"换手/成交量口径冲突(换手{today_turnover:.1f}%<{turnover_thresh:.0f}%, "
                f"成交量{today_volume / 1e8:.1f}亿股>{max_volume / 1e8:.0f}亿股)"
            )

    # Historical ratios exclude the current bar so intraday volume cannot move its own threshold.
    volume_ratios: List[float] = []
    for index in range(5, len(volumes) - 1):
        base = sum(volumes[index - 5:index]) / 5
        if base > 0:
            volume_ratios.append(volumes[index] / base)
    volume_ratios = volume_ratios[-min_samples:]

    turnover_history = turnover_values[-(min_samples + 1):-1] if len(turnover_values) > min_samples else []

    snapshot = VolumeSnapshot(
        volume_ratio=volume_ratio,
        volume_vs_ma60=volume_vs_ma60,
        dirty=dirty,
        dirty_reason=dirty_reason,
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
    if dirty:
        snapshot.label = "量能脏数据"

    # ============================================================
    # 【Phase4 P0-1】量能外推口径：盘中累计量 ÷ U型分位 → 全天量估计。
    # 午前累计量对比全天均量结构性偏小（分子只走了半天），
    # 蘅东光 9/7 实证：1.02x 拦截 → 外推口径 2.0x。
    # 禁用窗口：10:00 前（开盘冲量高估）/ 封板 / 14:45 后 / 非交易时段退化。
    # projection_time 可由调用方注入（回放/测试用），实盘用当前时钟。
    # ============================================================
    if volume_vs_ma60 is not None and volume_vs_ma60 > 0:
        try:
            from .volume_projection import project_volume_ratio
            _proj_time = None
            _override = tech_data.get("projection_time")
            if isinstance(_override, str) and ":" in _override:
                from datetime import datetime as _dt
                _proj_time = _dt.strptime(
                    f"2000-01-03 {_override}", "%Y-%m-%d %H:%M"
                )  # 2000-01-03 是周一，避免周末判定干扰
            elif _override is not None:
                _proj_time = _override
            _chg = tech_data.get("change_pct")
            # 封板判定用的涨跌停幅度：调用方按代码传入（北交所 30%，
            # 创科 20%，主板 10%）；缺省 0.10（保守）
            _limit = limit_ratio if limit_ratio is not None else tech_data.get("projection_limit_ratio")
            projection = project_volume_ratio(
                raw_ratio=volume_vs_ma60,
                change_pct=(None if _chg is None else float(_chg)),
                now=_proj_time,
                config=projection_config,
                limit_ratio=(float(_limit) if _limit is not None else 0.10),
            )
            snapshot.projected_volume_vs_ma60 = projection.projected_ratio
            snapshot.projection_mode = projection.mode
            snapshot.projection_note = projection.note
        except Exception as e:
            logger.debug("量能外推计算失败: %s", str(e)[:60])
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
    entry_type: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """策略感知的分档（层间接口修复：策略层→分档层语言对齐）。

    追强类（确认追强/价量突破/趋势延续）：本质是突破当日跟进，
    主档 = 触发位（Y 贴近现价），试探档 = 浅回踩（贴近触发位下方
    chase_probe_pct，MA5 更近时用 MA5）——挂在下方 10% 的 MA10 是
    低吸语义（蘅东光 9/8：Y=MA10 距现价 10.3%，RRR 0.96 的病根）。
    低吸类（恐慌抄底/套利低吸）：保留 MA10 主档 + MA5 试探档。
    """
    from .timing_engine import CHASE_STRATEGIES, DIP_STRATEGIES
    current = _number(tech_data.get("current_price"))
    ma5 = _number(tech_data.get("ma5"))
    ma10 = _number(tech_data.get("ma10"))
    chase_probe_pct = float(
        _config_get(config, "tiering", "chase_probe_pct", default=0.02)
    )
    tiers: List[Dict[str, Any]] = []
    is_chase = entry_type in CHASE_STRATEGIES
    if is_chase and benchmark_price > 0:
        # 追强主档：触发位跟进（Y = 现价/突破价）
        tiers.append(_execution_tier("追强档", "main", benchmark_price, current, "突破跟进"))
        # 浅回踩试探档：贴近触发位，绝不回撤到均线低吸位
        probe_price = benchmark_price * (1 - chase_probe_pct)
        if ma5 and 0 < ma5 < benchmark_price and ma5 >= probe_price:
            probe_price = ma5  # MA5 更贴近触发位时用 MA5
        tiers.append(_execution_tier("浅回踩档", "probe", probe_price, current, "缩量试探"))
    else:
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

    rsi6 = _number(tech_data.get("rsi6") or tech.get("rsi6"))
    if rsi6 is not None:
        if direction > 0 and rsi6 >= 70:
            conflicts.append({"label": "RSI过热", "severity": 1})
        elif direction < 0 and rsi6 <= 30:
            conflicts.append({"label": "RSI超卖", "severity": 1})

    divergence = tech.get("chan_divergence") or {}
    divergence_type = divergence.get("type")
    if divergence_type == "顶背驰" and direction > 0:
        conflicts.append({
            "label": "价格顶背驰"
                     + (f"({divergence.get('confidence')})" if divergence.get("confidence") else ""),
            "severity": 2 if divergence.get("confidence") == "高" else 1,
        })
    elif divergence_type == "底背驰" and direction < 0:
        conflicts.append({
            "label": "价格底背驰"
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


def _evaluate_ev_gate(
    entry_type: str,
    score: int,
    rrr_low: Optional[float],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """【评分层→决策层】期望值闸门：评分必须连闸门。

    依据（9/8 盘前验收）：蘅东光/罗博特科两条买入信号置信度 0/6 照样
    放行——EV = W×R−(1−W)，RRR0.96 隐含盈亏平衡胜率 51%，而系统自己
    给的信号质量是全池最低档，拿不出超过盈亏平衡线的胜率证据。
    规则：
      1. 评分低于 min_confidence_score（默认 1，即 0/6）→ 拒绝。
         0/6 不提供胜率证据，W 不能假设超过盈亏平衡线 → 负期望不出场，
         空仓是合法输出（决策记录 12 项里少数没动的）。
      2. 策略统计足样本（min_trades_for_win_rate，默认 30）时用真实
         胜率 W 算 EV = W×R−(1−W)；EV<0 → 拒绝。样本不足时用
         default_win_rate(0.5) 保守占位（不优于盈亏平衡，EV≤0）。
    作废条件：0/6 信号的分层统计期望为正且样本≥30 → 下调
    min_confidence_score（框架保留，参数重校）。
    """
    cfg = (config or {}).get("hypothesis_gate") or {}
    if cfg.get("enabled", True) is False:
        return {"enabled": False, "rejected": False, "reason": "期望值闸门关闭"}

    ev_cfg = cfg.get("ev_gate") or {}
    if ev_cfg.get("enabled", True) is False:
        return {"enabled": False, "rejected": False, "reason": "期望值子闸门关闭"}

    min_conf = int(ev_cfg.get("min_confidence_score", 2))
    min_trades = int(ev_cfg.get("min_trades_for_win_rate", 30))
    default_w = float(ev_cfg.get("default_win_rate", 0.5))

    breakeven = None
    if rrr_low is not None and rrr_low > 0:
        breakeven = 1.0 / (1.0 + rrr_low)

    # 策略真实胜率 W（分层统计足样本才采信；否则保守占位）
    win_rate = None
    trades = 0
    w_source = "无统计"
    try:
        from ..feedback.strategy_stats import compute_strategy_stats
        stats = compute_strategy_stats()
        bucket = (stats or {}).get(entry_type) or {}
        trades = int(bucket.get("trades") or 0)
        if trades >= min_trades and bucket.get("win_rate") is not None:
            win_rate = float(bucket["win_rate"])
            w_source = f"策略实测{trades}笔"
    except Exception:
        pass
    if win_rate is None:
        win_rate = default_w
        w_source = w_source if trades >= min_trades else f"保守占位{default_w:.0%}"

    ev = None
    if rrr_low is not None and rrr_low > 0:
        ev = win_rate * rrr_low - (1 - win_rate)

    rrr_txt = f"RRR{rrr_low:.2f}" if rrr_low is not None else "RRR缺失"
    be_txt = f"盈亏平衡胜率{breakeven * 100:.1f}%" if breakeven is not None else "盈亏平衡不可算"
    ev_txt = f"EV={ev:+.3f}" if ev is not None else "EV不可算"

    if score < min_conf:
        reason = (
            f"期望值闸门: 置信度{score}/6(全池最低档)不提供胜率证据，"
            f"{rrr_txt}，{be_txt}，系统不能假设超出盈亏平衡胜率 "
            f"→ 负期望不出场，空仓是合法输出"
        )
        return {
            "enabled": True, "rejected": True, "reason": reason,
            "score": score, "min_confidence_score": min_conf,
            "rrr_low": rrr_low, "breakeven_win_rate": breakeven,
            "win_rate": win_rate, "win_rate_source": w_source,
            "ev": ev, "trades": trades,
        }

    if ev is not None and ev < 0:
        reason = (
            f"期望值闸门: {w_source}胜率{win_rate*100:.1f}%，{rrr_txt}，"
            f"{ev_txt}<0 → 负期望不出场，空仓是合法输出"
        )
        return {
            "enabled": True, "rejected": True, "reason": reason,
            "score": score, "min_confidence_score": min_conf,
            "rrr_low": rrr_low, "breakeven_win_rate": breakeven,
            "win_rate": win_rate, "win_rate_source": w_source,
            "ev": ev, "trades": trades,
        }

    return {
        "enabled": True, "rejected": False, "reason": "期望值闸门通过",
        "score": score, "min_confidence_score": min_conf,
        "rrr_low": rrr_low, "breakeven_win_rate": breakeven,
        "win_rate": win_rate, "win_rate_source": w_source,
        "ev": ev, "trades": trades,
    }


def build_execution_plan(
    entry_type: str,
    benchmark_price: float,
    stop_loss: float,
    target_range: List[float],
    tech_data: Dict[str, Any],
    sector_status: str = "",
    sector_name: str = "",
    hypothesis_x: str = "",
    hypothesis: Optional[Dict[str, Any]] = None,
    atr: Optional[float] = None,
    data_guard: Optional[Dict[str, Any]] = None,
    gate_config: Optional[Dict[str, Any]] = None,
) -> ExecutionPlan:
    """构建执行计划（唯一出口）。

    【一】可证伪性出厂检查：X/Y/Z/W 缺任一要素、止损倒挂、缓冲不足、
    兑现无利润空间 → execute=False 拒绝（不进调度不推送），原因写入
    hard_constraint_notes / rejection_reasons 供留痕。
    原行为：倒挂仅降置信度照发（沃尔德 9/4: 买93.88/损93.94 照推）——
    新行为：该信号在生成阶段即被拒绝。
    """
    benchmark = _number(benchmark_price) or 0.0
    stop = _number(stop_loss) or 0.0
    targets = [_number(value) for value in (target_range or []) if _number(value) is not None]
    risk_abs = benchmark - stop if benchmark > 0 and stop > 0 and benchmark > stop else None
    risk_pct = risk_abs / benchmark if risk_abs is not None else None
    rewards = [(value - benchmark) / benchmark for value in targets if benchmark > 0 and value > benchmark]
    risk_ratio = risk_abs if risk_abs and risk_abs > 0 else None
    rrr_low = (targets[0] - benchmark) / risk_ratio if targets and risk_ratio else None
    rrr_high = (targets[-1] - benchmark) / risk_ratio if len(targets) >= 2 and risk_ratio else rrr_low

    volume = build_volume_snapshot(tech_data, guard=data_guard)
    fund = build_fund_snapshot(tech_data.get("institutional_holding"), tech_data)
    details: List[str] = []
    score = 0

    # ============================================================
    # 【一】假说出厂检查（先于置信度打分：缺要素的信号不配谈置信度）
    # ============================================================
    from .hypothesis import validate_hypothesis, TradeHypothesis

    hyp_obj = TradeHypothesis(
        strategy=str(entry_type or ""),
        reason_x=str(hypothesis_x or "").split("\n")[0].strip(),
        entry_y=benchmark,
        entry_y_note="主档回踩承接",
        exit_z=stop,
        exit_z_note="跌破止损价",
        exit_w=[t for t in targets if t],
        exit_w_note="到达目标位兑现",
    )
    # 若调用方已构建完整配对假说，直接复用其校验结果
    if hypothesis:
        hyp_obj = TradeHypothesis(
            strategy=hypothesis.get("strategy", entry_type or ""),
            reason_x=hypothesis.get("x", ""),
            entry_y=_number(hypothesis.get("y")) or benchmark,
            entry_y_note=hypothesis.get("y_note", ""),
            exit_z=_number(hypothesis.get("z")) or stop,
            exit_z_note=hypothesis.get("z_note", "跌破止损价"),
            exit_w=[_number(v) or 0.0 for v in (hypothesis.get("w") or [])],
            exit_w_note=hypothesis.get("w_note", "到达目标位兑现"),
            z_reference=_number(hypothesis.get("z_reference")) or 0.0,
            z_reference_name=hypothesis.get("z_reference_name", ""),
        )
    hyp_obj = validate_hypothesis(hyp_obj, atr=atr, config=gate_config)
    hypothesis_rejected = not hyp_obj.falsifiable

    # ============================================================
    # 【二】基本面闸门（Phase2-A）：业绩雷 veto / 盈利质量·财报窗口 warn
    # 买入理由 X 必须能与基本面共存——业绩在暴雷，
    # “放量突破”不构成买入理由；澜起式“净利+72%但扣非+21%”
    # 增长靠非经驱动 → 降级不否决。数据缺失时放行（不产生假基本面结论）。
    # ============================================================
    fundamental = tech_data.get("fundamental") or None
    fundamental_verdict: Optional[Dict[str, Any]] = None
    if fundamental:
        try:
            from .fundamental_gate import evaluate_fundamental_gate
            fundamental_verdict = evaluate_fundamental_gate(fundamental, config=gate_config)
            fundamental = {**fundamental, "verdict": fundamental_verdict}
        except Exception as e:
            logger.debug("基本面闸门评估失败: %s", str(e)[:60])
            fundamental_verdict = None
    fundamental_rejected = bool(
        fundamental_verdict and fundamental_verdict.get("verdict") == "veto"
    )

    # ============================================================
    # 【Phase3】估值透镜：高增长≠便宜。按 entry_type 重新评估
    # （追高型对 model_loss / 估值泡沫更严格）——
    # 长光华芯案例：PE 1155倍 + 净利仅3034万（低基数反转）→ veto；
    # 芯原案例：模式性亏损 + 确认追强 → veto；
    # 兆易案例：PE 33.9倍 + 净利 68.57亿 + +1091% → value_growth
    # 正向证据展示（不加分不否决，防系统性吹票）；
    # 数据全部缺失时放行（不产生假估值结论）。
    # ============================================================
    lens = tech_data.get("valuation_lens") or None
    valuation_verdict: Optional[Dict[str, Any]] = None
    valuation: Optional[Dict[str, Any]] = None
    if isinstance(lens, dict) and lens:
        try:
            from .valuation_lens import evaluate_valuation_lens
            lens_fund = lens.get("fundamental") or fundamental
            flow_vote = None
            inst = tech_data.get("institutional_holding")
            if isinstance(inst, dict):
                try:
                    flow_vote = int(inst.get("vote_score", 0) or 0)
                except (TypeError, ValueError):
                    flow_vote = None
            valuation_verdict = evaluate_valuation_lens(
                lens_fund, lens.get("valuation"), lens.get("analyst"),
                lens.get("balance"),
                entry_type=entry_type, config=gate_config, flow_vote=flow_vote,
            )
            valuation = dict(lens)
            valuation["verdict"] = valuation_verdict
        except Exception as e:
            logger.debug("估值透镜评估失败: %s", str(e)[:60])
            valuation_verdict = None
    valuation_rejected = bool(
        valuation_verdict and valuation_verdict.get("verdict") == "veto"
    )

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

    # 【Phase4 P0-2 + Phase5 回炉】RRR 质量票：RRR≥2.5 隐含胜率假设 28.6%
    # （1/(1+2.5)），显著优于 RRR1.5 的 40% 假设，够格拿一票——
    # 博杰 9/7 重算后置信度从 3/6 回到 4/6，不再被自己的评分体系错杀。
    # 【Phase5 钳制】RRR>cap（默认 5）不加分：分母过小制造的神话数字
    # 不构成质量证据（精智达 9/8 验收实证：RRR12.30 来自 0.84% 止损距离，
    # 日振幅 8.16% 的十分之一就能打穿——坏数据不配给好评）。
    # 阈值可配：confidence.rrr_quality_threshold / rrr_quality_cap。
    rrr_quality_threshold = float(
        _config_get(gate_config, "confidence", "rrr_quality_threshold", default=2.5)
    )
    rrr_quality_cap = float(
        _config_get(gate_config, "confidence", "rrr_quality_cap", default=5.0)
    )
    if rrr_low is not None and rrr_quality_threshold <= rrr_low <= rrr_quality_cap:
        score += 1
        details.append(f"RRR{rrr_low:.2f}∈[{rrr_quality_threshold:.1f},{rrr_quality_cap:.1f}]+1")
    elif rrr_low is not None and rrr_low > rrr_quality_cap:
        details.append(
            f"RRR{rrr_low:.2f}>{rrr_quality_cap:.1f}不加分(止损距离过窄，神话数字嫌疑)"
        )

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

    # 【评分层→决策层】期望值闸门：评分连闸门（0/6 低置信不再放行）。
    # 蘅东光/罗博特科 9/8：置信度 0/6 照样放行 → 本次修复把评分与决策
    # 焊死：评分低于 min_confidence_score 或按策略真实胜率算 EV<0 →
    # execute=False（负期望不出场，空仓是合法输出）。
    ev_verdict = _evaluate_ev_gate(entry_type, score, rrr_low, gate_config)
    ev_gate_rejected = bool(ev_verdict.get("rejected"))

    multipliers = {"base": 1.0}
    if volume.turnover_hot:
        multipliers["turnover_hot"] = 0.5
    if fund.shareholder_change_pct is not None and fund.shareholder_change_pct > 0.20:
        multipliers["shareholder_increase"] = 0.8
    # 【二】基本面 warn：盈利质量低/财报窗口 → 风险乘数（与 turnover_hot 同一体系）
    if fundamental_verdict and fundamental_verdict.get("verdict") == "warn":
        fv_mult = float(fundamental_verdict.get("risk_multiplier") or 0.6)
        multipliers["fundamental_warn"] = fv_mult
    # 【Phase3】估值透镜 warn：泡沫警示/低基数反转/亏损分型 → 风险乘数
    if valuation_verdict and valuation_verdict.get("verdict") == "warn":
        lv_mult = float(valuation_verdict.get("risk_multiplier") or 0.6)
        multipliers["valuation_warn"] = lv_mult
    # 【Phase5 回炉】波动率分档：验收实证（9/8）——精智达日振幅 8.16%、
    # 9/4 单日 -8.13%，日内噪声可扫损任何窄止损，风险系数却拿 1.00
    # （博杰 9/7 反而 0.60）——风险乘数不能随机出现，必须规则化。
    # 口径：max(当日振幅, 近 window 日平均振幅)，(high-low)/prev_close。
    # 反方（对冲）：低波动标的不降档（换手率已有 turnover_hot 单独惩罚；
    #   高波动本身不是错，错的是窄止损+高波动组合，Z 线缓冲另修）。
    # 作废条件：strategy_stats 分层统计显示 vol_tier 档期望值不低于
    #   全样本（高波动档不是劣勢来源）→ 移除分档。
    vol_tier_mult = _volatility_tier_multiplier(tech_data, gate_config)
    if vol_tier_mult is not None:
        multipliers["volatility_tier"] = vol_tier_mult
    combined = 1.0
    for multiplier in multipliers.values():
        combined *= multiplier

    industry_multiplier, industry_tags = _industry_tuning(sector_name)
    tiers = _build_execution_tiers(benchmark, stop, tech_data, volume, fund, entry_type, gate_config)

    # 【二】基本面 warn：置信度降一档（盈利质量低/财报窗口不否决，但降级）
    fundamental_warn = bool(fundamental_verdict and fundamental_verdict.get("verdict") == "warn")
    if fundamental_warn:
        confidence = {"高": "中", "中": "低", "低": "低"}.get(confidence, confidence)

    # 【Phase3】估值透镜 warn：置信度降一档（泡沫/低基数/亏损分型）
    valuation_warn = bool(valuation_verdict and valuation_verdict.get("verdict") == "warn")
    if valuation_warn:
        confidence = {"高": "中", "中": "低", "低": "低"}.get(confidence, confidence)

    # 【一】/【二】拒绝路径：假说不完整 或 基本面业绩雷 → execute=False（不进调度不推送，留痕）
    hard_notes: List[str] = list(hyp_obj.rejection_reasons)
    if hypothesis_rejected:
        hard_notes.insert(0, "假说四要素不完整，信号出厂即拒绝")
    if ev_gate_rejected:
        hard_notes.insert(0, str(ev_verdict.get("reason") or "期望值闸门拒绝"))
    fundamental_reasons = [f"基本面闸门: {r}" for r in (fundamental_verdict or {}).get("reasons") or []]
    if fundamental_rejected:
        hard_notes = fundamental_reasons + hard_notes
    elif fundamental_warn:
        hard_notes.extend(fundamental_reasons)
    valuation_reasons = [f"估值透镜: {r}" for r in (valuation_verdict or {}).get("reasons") or []]
    if valuation_rejected:
        hard_notes = valuation_reasons + hard_notes
    elif valuation_warn:
        hard_notes.extend(valuation_reasons)
    if hypothesis_rejected or fundamental_rejected or valuation_rejected or ev_gate_rejected:
        reject_details = list(details)
        if ev_gate_rejected:
            reject_details.append("期望值闸门拒绝（负期望不出场，空仓是合法输出）")
        if hypothesis_rejected:
            reject_details.append("假说被拒绝，不参与置信度评定")
        if fundamental_rejected:
            reject_details.append("基本面业绩雷，出厂即拒绝")
        if valuation_rejected:
            reject_details.append("估值透镜否决（泡沫+低基数/模式性亏损追高），出厂即拒绝")
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
            confidence="低",
            confidence_details=reject_details,
            volume_snapshot=volume,
            fund_snapshot=fund,
            risk_multipliers=multipliers,
            combined_risk_multiplier=combined,
            industry_multiplier=industry_multiplier,
            industry_tags=industry_tags,
            execution_tiers=tiers,
            hard_constraint_notes=hard_notes,
            execute=False,
            hypothesis=hyp_obj.as_dict(),
            hypothesis_rejected=hypothesis_rejected,
            rejection_reasons=list(hyp_obj.rejection_reasons) + fundamental_reasons + valuation_reasons
            + ([str(ev_verdict.get("reason") or "期望值闸门拒绝")] if ev_gate_rejected else []),
            fundamental=fundamental,
            fundamental_rejected=fundamental_rejected,
            valuation=valuation,
            valuation_rejected=valuation_rejected,
            ev_gate=ev_verdict,
        )
        return plan

    if fundamental_warn:
        details.append(f"基本面降级（{fundamental_verdict.get('note', '')}）")
    if valuation_warn:
        details.append(f"估值降级（{(valuation_verdict or {}).get('note', '')}）")

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
        hard_constraint_notes=hard_notes,
        execute=True,
        hypothesis=hyp_obj.as_dict(),
        hypothesis_rejected=False,
        rejection_reasons=[],
        fundamental=fundamental,
        fundamental_rejected=False,
        valuation=valuation,
        valuation_rejected=False,
        ev_gate=ev_verdict,
    )
    return plan


def confidence_score_value(label: str) -> int:
    return {"高": 4, "中": 2, "低": 0}.get(label, 0)


# ============================================================
# 【Phase5 回炉】波动率分档风险乘数 — 风险系数规则化
# ============================================================

def _volatility_tier_multiplier(
    tech_data: Dict[str, Any],
    gate_config: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """高波动标的的风险乘数分档（None = 不降档）。

    口径：max(当日振幅, 近 N 日平均振幅)，振幅 = (high - low) / prev_close。
    分档（默认，均可配 risk.volatility_tier）：
      - 振幅 ≥ 8%  → 0.6（精智达 9/8 验收实证：日振幅 8.16%）
      - 振幅 ≥ 5%  → 0.8
      - 振幅 < 5%  → None（不降档：换手率已有 turnover_hot 单独惩罚）
    数据缺失（无 K 线）→ None（不产生假波动结论）。
    作废条件：strategy_stats 分层统计显示 vol_tier 档期望不低于全样本 → 移除。
    """
    tier_cfg = (_config_get(gate_config, "risk", "volatility_tier") or {})
    if isinstance(tier_cfg, dict) and not tier_cfg.get("enabled", True):
        return None
    high_amp = float(tier_cfg.get("high_amp", 0.08) if tier_cfg else 0.08)
    mid_amp = float(tier_cfg.get("mid_amp", 0.05) if tier_cfg else 0.05)
    high_mult = float(tier_cfg.get("high_mult", 0.6) if tier_cfg else 0.6)
    mid_mult = float(tier_cfg.get("mid_mult", 0.8) if tier_cfg else 0.8)
    window = int(tier_cfg.get("window", 5) if tier_cfg else 5)

    kline = tech_data.get("kline") or []
    if not isinstance(kline, list) or len(kline) < 2:
        return None

    def _bar(k: Dict) -> Optional[Tuple[float, float, float]]:
        try:
            high = float(k.get("最高", k.get("high")) or 0)
            low = float(k.get("最低", k.get("low")) or 0)
            close = float(k.get("收盘", k.get("close")) or 0)
            if high > 0 and low > 0 and close > 0:
                return high, low, close
        except (TypeError, ValueError):
            pass
        return None

    bars = [_bar(k) for k in kline[-(window + 1):]]
    bars = [b for b in bars if b]
    if len(bars) < 2:
        return None

    amplitudes: List[float] = []
    for i in range(1, len(bars)):
        high, low, _ = bars[i]
        prev_close = bars[i - 1][2]
        if prev_close > 0:
            amplitudes.append((high - low) / prev_close)
    if not amplitudes:
        return None

    effective_amp = max(amplitudes[-1], sum(amplitudes) / len(amplitudes))
    if effective_amp >= high_amp:
        return high_mult
    if effective_amp >= mid_amp:
        return mid_mult
    return None


# ============================================================
# 【Phase4 P1-1】风险预算反推仓位（防守模式确认追强降仓可用）
# ============================================================

def compute_risk_budget_position(
    benchmark_price: float,
    stop_loss: float,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """试探仓比例 = 单笔风险预算 ÷ 止损距离（出处是风险预算，不是惯例）。

    设定单笔风险 ≤ 账户 risk_budget_pct（默认 1%）：
      仓位 ≈ 1% ÷ 3.5%（中波动结构位止损距离）≈ 29%（≈原仓位 1/3）；
      蘅东光类高波动标的（止损距离 ≈ 10%）→ 仓位 ≈ 10%。
    上限 max_probe_ratio（默认 1/3）—— 波动越小仓位越大，但封顶；
    止损距离异常小（<1%）时不放大仓位（防止毫厘止损杠杆化）。

    返回 {ratio, stop_distance_pct, budget_pct, capped, note}。
    """
    cfg = {
        "risk_budget_pct": 0.01,
        "max_probe_ratio": 1 / 3,
        "min_stop_distance_pct": 0.01,
    }
    if config and isinstance(config.get("defensive_chase"), dict):
        for key, default in list(cfg.items()):
            value = config["defensive_chase"].get(key)
            if isinstance(value, (int, float)) and value > 0:
                cfg[key] = float(value)

    benchmark = float(benchmark_price or 0)
    stop = float(stop_loss or 0)
    if benchmark <= 0 or stop <= 0 or stop >= benchmark:
        return {
            "ratio": 0.0, "stop_distance_pct": None, "budget_pct": cfg["risk_budget_pct"],
            "capped": False, "note": "买点/止损异常，仓位不可反推",
        }
    stop_distance = (benchmark - stop) / benchmark
    if stop_distance < cfg["min_stop_distance_pct"]:
        return {
            "ratio": 0.0, "stop_distance_pct": round(stop_distance, 4),
            "budget_pct": cfg["risk_budget_pct"], "capped": False,
            "note": f"止损距离{stop_distance*100:.1f}%异常小，不按预算反推（防毫厘止损杠杆化）",
        }
    raw_ratio = cfg["risk_budget_pct"] / stop_distance
    capped = raw_ratio > cfg["max_probe_ratio"]
    ratio = min(raw_ratio, cfg["max_probe_ratio"])
    return {
        "ratio": round(ratio, 4),
        "stop_distance_pct": round(stop_distance, 4),
        "budget_pct": cfg["risk_budget_pct"],
        "capped": capped,
        "note": (
            f"风险预算{cfg['risk_budget_pct']*100:.0f}%÷止损距离{stop_distance*100:.1f}%"
            f"={raw_ratio*100:.0f}%仓位"
            + (f"（封顶{cfg['max_probe_ratio']*100:.0f}%，中波动近似 1/3）" if capped else "")
        ),
    }
