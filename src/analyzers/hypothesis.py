"""
可证伪假说模型 — 每笔交易出厂前的完整性检查
================================================

一笔交易在执行前必须能写成这样的句子：

    "因为 X，所以在 Y 买入；如果 Z 出现，说明我错了，离场；如果 W 出现，兑现离场。"

X、Y、Z、W 四个位置缺一个，这笔交易就没有逻辑，只有冲动。
一个填不完整假说的信号，在生成阶段就应当被系统拒绝，而不是照常输出。

配对原则（买卖配对）：
1. Z 必须是 X 的直接否定 —— 突破买入的止损必须在突破位附近；
   底背驰买入的死亡条件就是背驰结构被新低破坏。
2. Z 的宽度由波动率决定 —— 止损距离至少 1.5~2 倍 ATR，
   避免沃尔德式 0.3% 乃至负缓冲的档位。
3. 买卖敏感度对称 —— 进场精确到 0.1% 挂单，出场就不能等 8~10% 外加四重投票。

四策略配对出场规格（X / Z / W）：

| 策略     | 买入理由 X            | 认错离场 Z                | 兑现离场 W                    |
|----------|----------------------|---------------------------|-------------------------------|
| 价量突破 | 放量站上关键位        | 收盘跌回突破位（跌回 MA25） | 下一阻力位分批，或 trailing 跟随 |
| 恐慌抄底 | 超跌+恐慌量能衰竭     | 反弹失败再创新低           | 反弹至密集套牢区减仓            |
| 套利低吸 | 周线趋势中的低位结构  | 低吸结构破位               | 回到趋势通道上沿               |
| 确认追强 | 强势股趋势确认        | 跌破趋势线/MA20            | 动能耗尽信号（顶背驰+缩量）     |
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 配对出场规格（策略级，数值阈值走 config/timing.yaml）
# ============================================================

STRATEGY_EXIT_SPECS: Dict[str, Dict[str, str]] = {
    "价量突破": {
        "z_rule": "收盘跌回突破位",
        "z_reference": "ma25",           # Z 锚 = 突破当日 MA25（突破位）
        "w_rule": "触及下一阻力位分批兑现，或 trailing(近5日低点)跟随",
        "w_reference": "recent_high",
    },
    "恐慌抄底": {
        "z_rule": "反弹失败再创新低（跌破恐慌低点）",
        "z_reference": "panic_low",      # Z 锚 = 信号时近10日低点/布林下轨
        "w_rule": "反弹至密集套牢区（近20日高点区域）减仓",
        "w_reference": "recent_high",
    },
    "套利低吸": {
        "z_rule": "低吸结构破位（跌破对子底/近5日低点结构）",
        "z_reference": "structure_low",  # Z 锚 = min(近5日低点, MA10)
        "w_rule": "回到趋势通道上沿（布林上轨附近）兑现",
        "w_reference": "boll_upper",
    },
    "确认追强": {
        "z_rule": "跌破趋势线/MA20",
        "z_reference": "ma20",
        "w_rule": "动能耗尽信号（顶背驰+缩量）或到达目标位分批兑现",
        "w_reference": "recent_high",
    },
}


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def calc_atr_from_kline(kline: List[Dict], period: int = 14) -> Optional[float]:
    """从 K 线计算 ATR（True Range 均值）。量纲：价格。"""
    if not kline or len(kline) < period + 1:
        return None
    try:
        highs = [float(k.get("最高", k.get("high", 0)) or 0) for k in kline[-(period + 1):]]
        lows = [float(k.get("最低", k.get("low", 0)) or 0) for k in kline[-(period + 1):]]
        closes = [float(k.get("收盘", k.get("close", 0)) or 0) for k in kline[-(period + 1):]]
        tr_list = []
        for i in range(1, len(highs)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            tr_list.append(tr)
        return sum(tr_list) / len(tr_list) if tr_list else None
    except (KeyError, ValueError, IndexError):
        return None


@dataclass
class TradeHypothesis:
    """一笔交易的可证伪假说（X/Y/Z/W 四要素）"""
    strategy: str                          # 策略名（= entry_type）
    reason_x: str = ""                     # X: 买入理由（必须自带死亡条件）
    entry_y: float = 0.0                   # Y: 买点（执行计划主档基准价）
    entry_y_note: str = ""                 # Y 触发条件描述
    exit_z: float = 0.0                    # Z: 认错离场价
    exit_z_note: str = ""                  # Z 触发条件描述
    exit_w: List[float] = field(default_factory=list)   # W: 兑现离场区间 [低, 高]
    exit_w_note: str = ""                  # W 触发条件描述
    z_reference: float = 0.0               # Z 锚定的结构位（突破位/趋势线/恐慌低点）
    z_reference_name: str = ""             # 结构位名称
    falsifiable: bool = True               # 完整性检查结论
    rejection_reasons: List[str] = field(default_factory=list)

    def sentence(self) -> str:
        """假说原句：推送到 trade_logs 与推送文案的原文。"""
        w_text = ""
        if self.exit_w:
            if len(self.exit_w) >= 2 and self.exit_w[1] > self.exit_w[0]:
                w_text = f"{self.exit_w[0]:.2f}-{self.exit_w[1]:.2f}"
            else:
                w_text = f"{self.exit_w[0]:.2f}"
        return (
            f"因为{self.reason_x}，所以在{self.entry_y:.2f}买入({self.entry_y_note})；"
            f"如果{self.exit_z_note}出现(Z={self.exit_z:.2f})，说明我错了，离场；"
            f"如果{self.exit_w_note}出现(W={w_text})，兑现离场。"
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "x": self.reason_x,
            "y": self.entry_y,
            "y_note": self.entry_y_note,
            "z": self.exit_z,
            "z_note": self.exit_z_note,
            "w": list(self.exit_w),
            "w_note": self.exit_w_note,
            "z_reference": self.z_reference,
            "z_reference_name": self.z_reference_name,
            "falsifiable": self.falsifiable,
            "rejection_reasons": list(self.rejection_reasons),
            "sentence": self.sentence() if self.falsifiable else "",
        }


def _config_get(config: Optional[Dict], *path, default=None):
    node = config or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _structure_reference(entry_type: str, tech_data: Dict) -> Tuple[float, str]:
    """按策略取 Z 锚定的结构位（X 的直接否定位置）。"""
    if entry_type == "价量突破":
        ref = _num(tech_data.get("ma25"))
        return (ref or 0.0, "突破位MA25")
    if entry_type == "确认追强":
        ref = _num(tech_data.get("ma20"))
        return (ref or 0.0, "趋势线MA20")
    if entry_type == "恐慌抄底":
        kline = tech_data.get("kline") or []
        lows = [_num(k.get("最低", k.get("low"))) for k in kline[-10:]]
        lows = [v for v in lows if v is not None]
        recent_low = min(lows) if lows else None
        boll = (tech_data.get("tech_signals") or {}).get("bollinger") or {}
        boll_lower = _num(boll.get("lower"))
        candidates = [v for v in (recent_low, boll_lower) if v is not None]
        ref = min(candidates) if candidates else None
        return (ref or 0.0, "恐慌低点(近10日低/布林下轨)")
    if entry_type == "套利低吸":
        kline = tech_data.get("kline") or []
        lows = [_num(k.get("最低", k.get("low"))) for k in kline[-5:]]
        lows = [v for v in lows if v is not None]
        recent_low = min(lows) if lows else None
        ma10 = _num(tech_data.get("ma10"))
        candidates = [v for v in (recent_low, ma10) if v is not None]
        ref = min(candidates) if candidates else None
        return (ref or 0.0, "低吸结构位(近5日低/MA10)")
    return (0.0, "")


def calculate_paired_stop(
    entry_type: str,
    tech_data: Dict,
    benchmark_price: float,
    atr: Optional[float],
    config: Optional[Dict] = None,
) -> Tuple[float, float, str]:
    """
    计算配对止损 Z（X 的直接否定 + 波动率宽度）。

    返回 (z_price, z_reference_price, z_note)。
    Z = 结构位 - max(z_atr_mult × ATR, 结构位 × z_pct_buffer)
      - 结构位 = X 死亡的位置（突破位/趋势线/恐慌低点/低吸结构位）
      - 宽度由波动率决定：至少 1.5×ATR（config hypothesis_gate.z_atr_mult）
      - ATR 缺失时退化为百分比缓冲（z_pct_buffer）
    """
    spec = STRATEGY_EXIT_SPECS.get(entry_type, {})
    z_rule = spec.get("z_rule", "跌破买点结构")
    ref, ref_name = _structure_reference(entry_type, tech_data)

    z_atr_mult = float(_config_get(config, "hypothesis_gate", "z_atr_mult", default=1.5))
    z_pct_buffer = float(_config_get(config, "hypothesis_gate", "z_pct_buffer", default=0.005))

    if ref <= 0:
        ref = float(benchmark_price or 0)

    buffer = 0.0
    if atr is not None and atr > 0:
        buffer = max(z_atr_mult * atr, ref * z_pct_buffer)
    else:
        buffer = ref * max(z_pct_buffer, 0.02)

    z_price = ref - buffer
    if ref > 0:
        z_price = min(z_price, ref * (1 - z_pct_buffer))  # Z 必须严格在结构位下方
    z_note = f"{z_rule}({ref_name}{ref:.2f}下方)"
    return round(z_price, 2), ref, z_note


def build_entry_hypothesis(
    entry_type: str,
    tech_data: Dict,
    benchmark_price: float,
    target_range: List[float],
    trigger_reason: str,
    atr: Optional[float] = None,
    config: Optional[Dict] = None,
    stop_loss_fallback: float = 0.0,
) -> TradeHypothesis:
    """
    构建入场假说（含配对 Z/W），随后必须经 validate_hypothesis 完整性检查。
    """
    spec = STRATEGY_EXIT_SPECS.get(entry_type, {})
    benchmark = float(benchmark_price or 0)

    z_price, z_ref, z_note = calculate_paired_stop(
        entry_type, tech_data, benchmark, atr, config
    )
    if z_price <= 0 and stop_loss_fallback > 0:
        z_price = float(stop_loss_fallback)

    targets = [_num(v) for v in (target_range or []) if _num(v) is not None]
    # W 必须在 Y 上方给出利润空间；不足时上抬 W 低点到成本上方最小利润位
    w_note = spec.get("w_rule", "到达目标位兑现")

    reason_x = str(trigger_reason or "").split("\n")[0].strip()
    if entry_type == "价量突破":
        y_note = "回踩确认不破"
    elif entry_type == "恐慌抄底":
        y_note = "恐慌盘口市价承接"
    elif entry_type == "套利低吸":
        y_note = "低位结构缩量承接"
    elif entry_type == "确认追强":
        y_note = "突破确认当日跟进"
    else:
        y_note = "主档回踩承接"

    hyp = TradeHypothesis(
        strategy=entry_type,
        reason_x=reason_x,
        entry_y=round(benchmark, 2),
        entry_y_note=y_note,
        exit_z=z_price,
        exit_z_note=z_note,
        exit_w=[round(t, 2) for t in targets],
        exit_w_note=w_note,
        z_reference=z_ref,
        z_reference_name=spec.get("z_reference", ""),
    )
    return validate_hypothesis(hyp, atr=atr, config=config)


def validate_hypothesis(
    hyp: TradeHypothesis,
    atr: Optional[float] = None,
    config: Optional[Dict] = None,
) -> TradeHypothesis:
    """
    假说完整性检查（出厂检查）。缺任一要素或 Z/Y 倒挂 → falsifiable=False。

    检查项：
      1. X 非空（买入理由必须自带死亡条件）
      2. Y > 0（买点）
      3. Z > 0 且 Z < Y（认错离场 —— 沃尔德式"止损93.94>买点93.88"在此拦截）
      4. Z 宽度：Y - Z >= max(z_atr_mult×ATR, min_z_buffer_pct×Y)
      5. W 非空且 W 高点 > Y（兑现离场必须有利润空间）
    """
    reasons: List[str] = []
    gate_enabled = bool(_config_get(config, "hypothesis_gate", "enabled", default=True))
    if not gate_enabled:
        hyp.falsifiable = True
        hyp.rejection_reasons = []
        return hyp

    if not hyp.reason_x:
        reasons.append("买入理由缺失(X): 无理由即无死亡条件，不算交易")
    if hyp.entry_y <= 0:
        reasons.append("买点缺失(Y): 无法定义入场基准")
    if hyp.exit_z <= 0:
        reasons.append("认错离场缺失(Z): 没有死亡条件的理由不算理由")
    if hyp.entry_y > 0 and hyp.exit_z > 0 and hyp.exit_z >= hyp.entry_y:
        reasons.append(
            f"止损倒挂(Z>=Y): 认错价{hyp.exit_z:.2f}高于买点{hyp.entry_y:.2f}，"
            "假说自相矛盾"
        )
    if hyp.entry_y > 0 and hyp.exit_z > 0 and hyp.exit_z < hyp.entry_y:
        z_atr_mult = float(_config_get(config, "hypothesis_gate", "z_atr_mult", default=1.5))
        min_pct = float(_config_get(config, "hypothesis_gate", "min_z_buffer_pct", default=0.01))
        if atr is not None and atr > 0:
            min_width = z_atr_mult * atr
            source = f"{z_atr_mult:.1f}×ATR({atr:.2f})"
        else:
            min_width = hyp.entry_y * min_pct
            source = f"{min_pct * 100:.1f}%最小缓冲"
        width = hyp.entry_y - hyp.exit_z
        if width < min_width:
            reasons.append(
                f"止损缓冲不足(Z宽度): 买点-认错价仅{width:.2f}"
                f"({width / hyp.entry_y * 100:.2f}%)，低于{source}={min_width:.2f}，"
                "易被正常波动扫损"
            )
    w_valid = [v for v in hyp.exit_w if v and v > 0]
    if not w_valid:
        reasons.append("兑现离场缺失(W): 无目标即无兑现纪律")
    elif hyp.entry_y > 0 and max(w_valid) <= hyp.entry_y * 1.005:
        reasons.append(
            f"兑现目标无利润空间(W): 最高目标{max(w_valid):.2f}未高于买点{hyp.entry_y:.2f}"
        )

    hyp.rejection_reasons = reasons
    hyp.falsifiable = not reasons
    if not hyp.falsifiable:
        logger.info("假说被拒绝 [%s]: %s", hyp.strategy, "; ".join(reasons))
    return hyp


def new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def expire_date_from(born: date, valid_days: int) -> str:
    return (born + timedelta(days=valid_days)).isoformat()
