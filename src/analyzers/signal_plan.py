"""Single-source execution planning for entry signals."""

import logging
from datetime import date
from pathlib import Path

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _scoring_weights() -> dict:
    try:
        import yaml
        from pathlib import Path
        p = Path(__file__).parent.parent.parent / "config" / "scoring_weights.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f).get("confidence", {})
    except Exception:
        pass
    return {}

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
    volume_ratio_source: str = "数据未取到"
    volume_ratio_raw: Optional[float] = None
    volume_vs_prev_day: Optional[float] = None
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
    # v2.9: number + caliber + source + sample time are inseparable.
    volume_ratio_caliber: str = "口径未标注"
    volume_ratio_sample_time: Optional[str] = None
    same_period_volume_ratio: Optional[float] = None
    same_period_caliber: str = "数据未取到"
    same_period_sample_time: Optional[str] = None
    volume_ratio_effective: Optional[float] = None
    is_early_window: bool = False
    kline_cumulative_volume_ratio: Optional[float] = None
    volume_caliber_conflict: bool = False
    volume_caliber_conflict_note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FundSnapshot:
    main_flows: List[float] = field(default_factory=list)
    main_total: Optional[float] = None
    main_vote: int = 0
    main_strong: bool = False
    flow_coverage_days: int = 0
    flow_expected_days: int = 0
    flow_coverage_complete: bool = False
    source_conflict: bool = False
    cross_source: str = ""
    cross_total: Optional[float] = None
    fund_tech_conflict: bool = False
    fund_tech_conflict_note: str = ""
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
    margin_drive_conflict: bool = False
    margin_drive_conflict_note: str = ""
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
    strategy_benchmark_price: float = 0.0
    benchmark_source: str = "strategy"
    benchmark_gap_pct: Optional[float] = None
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
    rejection_reasons: List[str] = field(default_factory=list)
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


def _last_bar_date(item: Dict[str, Any]) -> str:
    return str(item.get("date", item.get("日期", "")))[:10]


def get_history_excluding_today(
    kline: List[Dict[str, Any]],
    today_volume: Optional[float] = None,
    today_date: Optional[str] = None,
    kline_includes_today: Optional[bool] = None,
) -> List[float]:
    """返回历史成交量序列；若最后一根是今日 bar，则剔除今日。

    盘中实时行情会被同步进最后一根 K 线。60日均量、5日均量等“历史基准”
    不能用当日盘中累计量抬高分母，否则放量会自我稀释。
    """
    volumes = [
        _number(item.get("volume", item.get("成交量", 0))) or 0.0
        for item in kline
    ]
    if not volumes:
        return []
    last_date = _last_bar_date(kline[-1]) if kline else ""
    if kline_includes_today is None:
        last_is_today = bool(
            (today_date and last_date == today_date)
            or (
                today_volume is not None
                and _number(volumes[-1]) == today_volume
            )
        )
    else:
        last_is_today = bool(kline_includes_today)
    return volumes[:-1] if last_is_today else volumes


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
    volume_ratio_source = str(tech_data.get("volume_ratio_source") or "K线5日均量")
    volume_ratio_raw = _number(tech_data.get("volume_ratio_raw"))
    if volume_ratio_raw is None:
        volume_ratio_raw = volume_ratio
    today_date = tech_data.get("data_date") or tech_data.get("quote_date")
    history_volumes = get_history_excluding_today(
        kline,
        today_volume=today_volume,
        today_date=today_date,
        kline_includes_today=tech_data.get("kline_includes_today"),
    )
    volume_ma60 = None
    if len(history_volumes) >= 60:
        volume_ma60 = sum(history_volumes[-60:]) / 60
    supplied_volume_ma60 = _number(tech_data.get("volume_ma60"))
    if supplied_volume_ma60 is not None and len(history_volumes) < 60:
        volume_ma60 = supplied_volume_ma60
    volume_vs_ma60 = (
        today_volume / volume_ma60
        if today_volume and volume_ma60 and volume_ma60 > 0
        else None
    )
    prev_day_volume = (
        history_volumes[-1]
        if len(history_volumes) >= 1 and history_volumes[-1] > 0
        else None
    )
    kline_cumulative_volume_ratio = None
    if today_volume and len(history_volumes) >= 5:
        base5 = sum(history_volumes[-5:]) / 5
        if base5 > 0:
            kline_cumulative_volume_ratio = today_volume / base5
    volume_vs_prev_day = (
        today_volume / prev_day_volume
        if today_volume and prev_day_volume and prev_day_volume > 0
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
        volume_ratio_source=volume_ratio_source,
        volume_ratio_raw=volume_ratio_raw,
        volume_vs_prev_day=volume_vs_prev_day,
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
    snapshot.kline_cumulative_volume_ratio = kline_cumulative_volume_ratio

    sample_time = tech_data.get("volume_ratio_sample_time")
    if sample_time is not None:
        snapshot.volume_ratio_sample_time = str(sample_time)
    if volume_ratio_source == "行情接口":
        snapshot.volume_ratio_caliber = "接口量比(5日分钟均量)"
    elif volume_ratio_source:
        snapshot.volume_ratio_caliber = str(volume_ratio_source)

    try:
        from .volume_projection import (
            parse_sample_time,
            same_period_ratio_from_interface,
            trading_minute_fraction_at,
        )
        _time_text = snapshot.volume_ratio_sample_time
        _sample_dt = parse_sample_time(_time_text)
        if volume_ratio_source == "行情接口" and volume_ratio_raw is not None:
            snapshot.same_period_volume_ratio = same_period_ratio_from_interface(
                volume_ratio_raw, sample_time=_sample_dt or _time_text,
                config=projection_config,
            )
            snapshot.same_period_sample_time = snapshot.volume_ratio_sample_time
            snapshot.same_period_caliber = (
                "同期累计量比(5日同期,U曲线校准)"
                if snapshot.same_period_volume_ratio is not None
                else "同期量比数据未取到"
            )
        _minute_fraction = (
            trading_minute_fraction_at(_sample_dt) if _sample_dt is not None else None
        )
        if _minute_fraction is not None:
            snapshot.is_early_window = (
                0 < _minute_fraction < (10 * 60 + 30 - 9 * 60 - 30) / 240.0
            )
        if snapshot.is_early_window or (
            volume_ratio_source == "行情接口" and not snapshot.volume_ratio_sample_time
        ):
            # 早盘缺失同期口径、或接口量比缺采样时间时必须诚实降级，
            # 接口原始量比不得回流为正式证据。
            snapshot.volume_ratio_effective = snapshot.same_period_volume_ratio
        else:
            snapshot.volume_ratio_effective = volume_ratio

        if (
            volume_ratio_source == "行情接口"
            and volume_ratio_raw is not None
            and kline_cumulative_volume_ratio is not None
            and _minute_fraction
        ):
            expected = volume_ratio_raw * _minute_fraction
            deviation = abs(kline_cumulative_volume_ratio - expected) / expected
            if deviation >= 0.25:
                snapshot.volume_caliber_conflict = True
                snapshot.volume_caliber_conflict_note = (
                    f"量能口径对账异常: K线累计{kline_cumulative_volume_ratio:.2f}x "
                    f"vs 接口同期折算{expected:.2f}x(偏差{deviation:.0%})"
                )
    except Exception as e:
        logger.debug("量能同期口径计算失败: %s", str(e)[:60])

    has_turnover = (
        snapshot.turnover_rate is not None
        and snapshot.turnover_p25 is not None
        and snapshot.turnover_p90 is not None
    )
    evidence_ratio = (
        snapshot.volume_ratio_effective
        if snapshot.is_early_window
        else snapshot.volume_ratio
    )
    has_volume = (
        evidence_ratio is not None
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
        and evidence_ratio is not None
        and evidence_ratio > (snapshot.volume_ratio_p90 or 0)
    )
    snapshot.shrinking = bool(
        has_volume
        and evidence_ratio is not None
        and evidence_ratio < (snapshot.volume_ratio_p25 or 0)
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


def _order_flow_drive(tech_data: Dict[str, Any]) -> Optional[float]:
    """从 tech_data 提取主动差 D（小数，imbalance_pct %→/100）。缺失返回 None。"""
    flow = ((tech_data or {}).get("tech_signals") or {}).get("order_flow") or {}
    if not flow.get("available"):
        return None
    imbalance = _number(flow.get("imbalance_pct"))
    if imbalance is None:
        return None
    return imbalance / 100.0


def margin_drive_adjudication(
    institutional: Optional[Dict[str, Any]],
    drive: Optional[float],
) -> Tuple[bool, str]:
    """P1-8 两融/主动差背离裁决。

    两融变动>5% 且 主动差<-5%（或反向显著）→ (True, 裁决行文案)。
    两融为 T-1 滞后披露、主动差为当日实时成交，冲突时以主动差为准。
    无冲突/数据不足 → (False, "")。
    """
    if drive is None:
        return False, ""
    votes = (institutional or {}).get("votes") or {}
    raw = (votes.get("north_bound") or {}).get("raw") or {}
    margin_change = _number(raw.get("change_pct"))
    if margin_change is None:
        return False, ""
    if margin_change > 0.05 and drive < -0.05:
        return True, (
            f"裁决:两融逆势加杠杆({margin_change * 100:+.1f}%) vs 主动卖出"
            f"(主动差{drive * 100:+.1f}%)——两融T-1滞后披露,以当日主动差为准,"
            "当日不追,等资金回补确认"
        )
    if margin_change < -0.05 and drive > 0.05:
        return True, (
            f"裁决:两融撤退({margin_change * 100:+.1f}%) vs 主动买入"
            f"(主动差{drive * 100:+.1f}%)——两融T-1滞后披露,以当日主动差为准,"
            "杠杆撤退不单独否决"
        )
    return False, ""


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

    source = str(raw.get("source") or "")
    coverage_days = int(_number(raw.get("coverage_days")) or len(main_flows))
    expected_days = int(
        _number(raw.get("expected_days"))
        or (3 if "3日" in source else 5 if "5日" in source else len(main_flows))
    )
    coverage_complete = bool(expected_days > 0 and coverage_days >= expected_days)
    main_vote = int(main.get("vote", 0) or 0) if coverage_complete else 0
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
    if not coverage_complete and expected_days > 0:
        machine_tags.append(f"资金覆盖{coverage_days}/{expected_days}日,部分覆盖降权")
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

    source_conflict = bool(raw.get("source_conflict"))
    cross_source = str(raw.get("cross_source") or "")
    cross_total = _number(raw.get("cross_total"))
    if source_conflict:
        machine_tags.append("资金源冲突")

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

    tech_score = _net_technical_votes(tech_data or {})
    conflict_note = ""
    fund_tech_conflict = False
    if (
        main_total is not None
        and coverage_complete
        and tech_score is not None
        and (
            (main_total > 0 and tech_score < 0)
            or (main_total < 0 and tech_score > 0)
        )
    ):
        float_market_cap = _number(
            ((tech_data or {}).get("valuation") or {}).get("float_market_cap")
            or ((tech_data or {}).get("valuation") or {}).get("market_cap")
        )
        cap_value = float_market_cap * 1e8 if float_market_cap else None
        large = abs(main_total) > 5e8 or (cap_value and abs(main_total) > cap_value * 0.005)
        if large:
            fund_tech_conflict = True
            conflict_note = (
                f"资金-技术冲突: 主力{expected_days}日{main_total / 1e8:+.2f}亿 vs 技术{tech_score:+.1f}; "
                "技术右侧规则优先，大额资金只作环境观察，不单独解除入场闸门"
            )
            machine_tags.append("资金-技术冲突")

    # P1-8 两融/主动差背离裁决：两融变动>5% 且 主动差<-5%（或反向显著）时，
    # 拦截栏强制输出裁决行（优先级+依据），禁止两源矛盾静默陈列。
    drive = _order_flow_drive(tech_data or {})
    margin_drive_conflict, margin_drive_conflict_note = margin_drive_adjudication(
        institutional, drive,
    )
    if margin_drive_conflict:
        machine_tags.append("两融/主动差背离")

    return FundSnapshot(
        main_flows=main_flows,
        main_total=main_total,
        main_vote=main_vote,
        main_strong=main_strong,
        flow_coverage_days=coverage_days,
        flow_expected_days=expected_days,
        flow_coverage_complete=coverage_complete,
        source_conflict=source_conflict,
        cross_source=cross_source,
        cross_total=cross_total,
        fund_tech_conflict=fund_tech_conflict,
        fund_tech_conflict_note=conflict_note,
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
        margin_drive_conflict=margin_drive_conflict,
        margin_drive_conflict_note=margin_drive_conflict_note,
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



def build_execution_plan(
    entry_type: str,
    benchmark_price: float,
    stop_loss: float,
    target_range: List[float],
    tech_data: Dict[str, Any],
    sector_status: str = "",
    sector_name: str = "",
    atr: Optional[float] = None,
    data_guard: Optional[Dict[str, Any]] = None,
    gate_config: Optional[Dict[str, Any]] = None,
) -> ExecutionPlan:
    """构建执行计划（唯一出口）。"""
    benchmark = _number(benchmark_price) or 0.0
    stop = _number(stop_loss) or 0.0
    strategy_benchmark = benchmark
    benchmark_source = "strategy"
    benchmark_gap_pct = None
    current_price = _number(tech_data.get("current_price"))
    stale_threshold = float(_config_get(
        gate_config, "execution_plan", "benchmark_refresh_gap_pct", default=0.10
    ))
    if (
        current_price is not None
        and current_price > 0
        and benchmark > 0
        and 0.5 <= current_price / benchmark <= 2.0
        and stale_threshold >= 0
        and abs(current_price / benchmark - 1.0) > stale_threshold
    ):
        benchmark_gap_pct = current_price / benchmark - 1.0
        benchmark = current_price
        benchmark_source = "current_price_refresh"
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

    _sw = _scoring_weights()
    _w_tech = int(_sw.get("tech_alignment_bonus", 1))
    _w_inst = int(_sw.get("institutional_alignment_bonus", 1))
    _w_main = int(_sw.get("main_trend_bonus", 1))
    _w_no_conflict = int(_sw.get("no_conflict_bonus", 1))
    _w_strong_conflict = int(_sw.get("strong_conflict_penalty", -1))
    _w_market = int(_sw.get("market_score_bonus", 1))
    _w_market_threshold = int(_sw.get("market_score_threshold", 6))

    technical_votes = _net_technical_votes(tech_data)
    if technical_votes is not None and technical_votes * 1 >= 2:
        score += _w_tech
        details.append(f"技术同向+{_w_tech}(①②③合成)")
    elif technical_votes is not None:
        details.append(f"技术票{technical_votes:+.1f}(①②③合成)")
    else:
        details.append("技术票缺失")

    institutional_score = fund.institutional_score + fund.institutional_adjustment
    if institutional_score >= 2:
        score += _w_inst
        details.append(f"机构同向+{_w_inst}")
    else:
        details.append(f"机构票{institutional_score:+d}")

    if sector_status == "main_trend":
        score += _w_main
        details.append(f"主线+{_w_main}")
    else:
        details.append("非主线")

    conflicts = _signal_conflicts(tech_data)
    conflict_labels = [str(item["label"]) for item in conflicts]
    strong_conflicts = [item for item in conflicts if int(item["severity"]) >= 2]
    if not conflicts:
        score += _w_no_conflict
        details.append(f"无矛盾+{_w_no_conflict}")
    elif strong_conflicts or len(conflicts) >= 2:
        score += _w_strong_conflict
        details.append(f"矛盾{_w_strong_conflict:+d}:" + "/".join(conflict_labels))
    else:
        details.append("矛盾0:" + "/".join(conflict_labels))

    market_score = _number(tech_data.get("market_score"))
    if market_score is not None:
        if market_score >= _w_market_threshold:
            score += _w_market
            details.append(f"环境{market_score:.1f}+{_w_market}")
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
    tiers = _build_execution_tiers(benchmark, stop, tech_data, volume, fund, entry_type, gate_config)

    plan = ExecutionPlan(
        entry_type=entry_type,
        benchmark_price=benchmark,
        strategy_benchmark_price=strategy_benchmark,
        benchmark_source=benchmark_source,
        benchmark_gap_pct=benchmark_gap_pct,
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
        rejection_reasons=[],
    )
    return plan


def confidence_score_value(label: str) -> int:
    return {"高": 4, "中": 2, "低": 0}.get(label, 0)
