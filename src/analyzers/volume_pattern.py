"""
③量能分型引擎 — 把并列陈列的盘口指标组合成可执行的盘口诊断。

背景（2026-09-14 改造）：原③量能只做陈列——量比、换手、内外盘各报各的，
"内外盘(+2.5%,展示)"不参与任何判定。老手收盘后翻自选时做的是组合判定：
涨跌方向 × 内外盘方向 × 量能档 × 位置档 → 分型。本模块把这套直觉写成规则。

四个计算字段（数据全部来自报告已有字段）：
  D 主动差 = (外盘-内盘)/(外盘+内盘)，沿用 order_flow.imbalance_pct（%→小数）
  V 量能档 = 量比分档（缩量/平量/温和放量/明显放量/剧烈放量）
  换手档   = 换手率分档（清淡/正常/活跃/过热/决战）——量比0.89配6.5%换手
             与配1%换手是完全不同的市场状态，"缩量"一词必须由换手校准
  P 位置档 = 高位/中继/破位/低位（复用三重门的 prior_high 与①的 MA 排列）

八型分型矩阵（按序匹配，先命中先输出）：
  跌 D>0 剧烈放量 高位        → 对倒诱多嫌疑（外盘不可信，先假设派发）
  跌 D>0 放量系 低位/中继     → 资金接货型（主动买盘在接，看均价线）
  跌 D>0 缩量/平量            → 抛压衰竭型（跌不动了，缺一根放量确认）
  跌 D≤0 放量系               → 真实抛压型（主动性出逃，防加速破位）
  跌 D≤0 缩量/平量            → 阴跌不止型（趋势未止，等右侧）
  涨/平 D<0                   → 承接型上涨（靠挂单接，用均价线辨真伪）
  涨/平 D>=0 剧烈放量 高位    → 高位滞涨派发（放巨量而不涨，派发前兆）
  涨/平 D>=0 其余             → 健康推进型（方向与力度共振）
（D==0 归入内盘侧：均衡即无主动接盘，按保守侧处置）

冲突检测（量价 × 资金交叉引用，解决③与④互不引用）：
  规则一 分型偏多 且 (资金投票偏空 或 主力3日净流出)
         → ⚠量价资金背离：主动买盘疑似散单，降一级观察
  规则二 分型偏空 且 资金投票偏多 → ⚠资金逆势：次日验证优先

委比交叉验证（P1，腾讯行情五档已含挂单量，字段缺失时静默跳过）：
  委比与主动差同向 → 委托一致；反向 → 托价嫌疑 / 压单吸筹或边拉边撤。

失效豁免：涨跌停封板附近（|涨跌幅|≥9.8% 代理）内外盘失真，跳过分型。
  次新/除权日/停牌复牌首日/尾盘集合竞价量比骤变需上市日历与分时数据，
  数据接入前不豁免（诚实缺失，不伪造判定）。

二期预留：收盘价相对当日均价偏离、早盘/尾盘分段内外盘（需分钟线）。
"""
from typing import Dict, List, Optional

# ── 量能档阈值（量比） ──
# 缩量边界校准到 9/11 飞荣达验收样例：量比0.89 标"缩量"
# （方案伪代码 0.8 与示例 0.89→缩量 冲突，以示例验收口径为准）
_VOLUME_GEARS = ((0.9, "缩量"), (1.2, "平量"), (2.5, "温和放量"), (3.5, "明显放量"))
VOLUME_EXTREME_GEAR = "剧烈放量"

# ── 换手档阈值（%） ──
_TURNOVER_GEARS = ((1.0, "清淡"), (3.0, "正常"), (7.0, "活跃"), (15.0, "过热"))
TURNOVER_EXTREME_GEAR = "决战"

# ── 位置档阈值 ──
NEAR_HIGH_MAX = 0.10      # 距前高 ≤10% 视为"贴前高"
GAIN_20D_HIGH = 0.30      # 且近20日涨幅 >30% → 高位

# ── 委比有效门槛（%）：五档挂单在远离盘口时噪音大，太小不判同向/反向 ──
WB_SIGNIFICANT_PCT = 10.0

# ── 失效豁免：封板代理判定留 0.2% 边距（精确封板需分时/逐笔，二期） ──
LIMIT_PROXY_MARGIN = 0.2

# 八型定义：bias +1偏多 / -1偏空 / 0中性（冲突检测与一览行共用）
_PATTERN_DEFS = {
    "抛压衰竭型": {
        "short": "抛压衰竭", "bias": 1,
        "verdict": "跌不动了，缺一根放量确认",
        "strategy": "低吸观察池", "strategy_short": "低吸观察",
        "confirms": ["量比回升1.2+", "收盘站上MA5", "外盘持续占优"],
        "confirm_target": "触发低吸复核",
    },
    "资金接货型": {
        "short": "资金接货", "bias": 1,
        "verdict": "主动买盘在接，看均价线",
        "strategy": "低吸候选", "strategy_short": "低吸候选",
        "confirms": ["量比维持1.2+", "收盘站上MA5", "主力3日净流转正"],
        "confirm_target": "触发低吸复核",
    },
    "对倒诱多嫌疑": {
        "short": "对倒诱多", "bias": -1,
        "verdict": "外盘不可信，先假设派发",
        "strategy": "回避", "strategy_short": "回避",
        "confirms": ["外盘/内盘回到均衡(对倒消退)", "主力净流出扩大", "跌破今日低点即执行回避"],
        "confirm_target": "执行回避",
    },
    "真实抛压型": {
        "short": "真实抛压", "bias": -1,
        "verdict": "主动性出逃，防加速破位",
        "strategy": "风控优先", "strategy_short": "风控",
        "confirms": ["反弹量能不萎缩则继续回避", "主力净流出扩大", "跌破MA20减仓"],
        "confirm_target": "风控优先",
    },
    "阴跌不止型": {
        "short": "缩量阴跌", "bias": -1,
        "verdict": "趋势未止，等右侧",
        "strategy": "等待", "strategy_short": "等待",
        "confirms": ["放量止跌(量比1.5+)", "收盘收复MA5", "外盘转占优"],
        "confirm_target": "等右侧信号",
    },
    "承接型上涨": {
        "short": "承接上涨", "bias": 0,
        "verdict": "靠挂单接，用均价线辨真伪",
        "strategy": "看均价线定去留", "strategy_short": "看均价线",
        "confirms": ["主力净流转正", "量比回升1.0+", "收盘站上MA5"],
        "confirm_target": "均价线辨真伪(分时二期接入)",
    },
    "健康推进型": {
        "short": "健康推进", "bias": 1,
        "verdict": "方向与力度共振",
        "strategy": "持有/追强复核", "strategy_short": "持有复核",
        "confirms": ["量比维持1.0+", "回踩不破MA5", "外盘持续占优"],
        "confirm_target": "持有复核",
    },
    "高位滞涨派发": {
        "short": "高位派发", "bias": -1,
        "verdict": "放巨量而不涨，派发前兆",
        "strategy": "减仓", "strategy_short": "减仓",
        "confirms": ["缩量回踩不破MA5", "外盘占比转均衡", "破MA5减仓"],
        "confirm_target": "执行减仓",
    },
}

# 分型偏多/偏空集合（冲突检测规则一/二的"分型偏X"）
_BULLISH_PATTERNS = {"抛压衰竭型", "资金接货型", "健康推进型"}
_BEARISH_PATTERNS = {"真实抛压型", "高位滞涨派发", "对倒诱多嫌疑"}


def volume_gear(volume_ratio) -> Optional[str]:
    """量比 → 量能档。档界按方案伪代码：缩量<0.9(校准至验收样例0.89)、
    ≤1.2平量、≤2.5温和放量、≤3.5明显放量、其余剧烈放量。缺失返回 None。"""
    try:
        vr = float(volume_ratio)
    except (TypeError, ValueError):
        return None
    if vr != vr:  # NaN
        return None
    if vr < _VOLUME_GEARS[0][0]:
        return _VOLUME_GEARS[0][1]
    for threshold, label in _VOLUME_GEARS[1:]:
        if vr <= threshold:
            return label
    return VOLUME_EXTREME_GEAR


def turnover_gear(turnover_pct) -> Optional[str]:
    """换手率(%) → 换手档。缺失返回 None。"""
    try:
        t = float(turnover_pct)
    except (TypeError, ValueError):
        return None
    if t != t:
        return None
    for threshold, label in _TURNOVER_GEARS:
        if t < threshold:
            return label
    return TURNOVER_EXTREME_GEAR


def active_drive(flow: Optional[Dict]) -> Optional[float]:
    """内外盘 → 主动差 D ∈ [-1, 1]。D>0 外盘占优。数据缺失返回 None。"""
    if not isinstance(flow, dict) or not flow.get("available"):
        return None
    imbalance = flow.get("imbalance_pct")
    if imbalance is None:
        return None
    try:
        d = float(imbalance) / 100.0
    except (TypeError, ValueError):
        return None
    return d if d == d else None


def drive_label(drive: Optional[float]) -> str:
    """主动差方向标签：与分型矩阵同符号口径（D>0 即外盘占优，
    不套 tech_signals.order_flow.label 的 ±5% 带宽——矩阵只看方向）。
    飞荣达验收样例：+2.5% → 外盘占优。"""
    if drive is None:
        return "数据未取到"
    if drive > 0:
        return "外盘占优"
    if drive < 0:
        return "内盘占优"
    return "均衡"


def position_gear(close, ma5, ma10, ma20, near_high_pct,
                  gain_20d, ma20_falling) -> str:
    """位置档：高位 / 中继 / 破位 / 低位。

    高位 = 贴前高(≤10%) 且 近20日涨幅>30%（复用三重门 prior_high 口径）；
    中继 = MA多头排列；破位 = 收盘<MA20 且 MA20向下（斜率缺失时按收盘<MA20
    判，并在 confirm 里提示核对斜率——诚实降级好过静默跳过）；其余 = 低位。
    """
    if (near_high_pct is not None and near_high_pct <= NEAR_HIGH_MAX
            and gain_20d is not None and gain_20d > GAIN_20D_HIGH):
        return "高位"
    try:
        if ma5 and ma10 and ma20 and float(ma5) > float(ma10) > float(ma20):
            return "中继"
    except (TypeError, ValueError):
        pass
    try:
        if ma20 and close and float(close) < float(ma20) and ma20_falling is not False:
            return "破位"
    except (TypeError, ValueError):
        pass
    return "低位"


def classify(change_pct, drive: Optional[float], vgear: Optional[str],
             position: str) -> Optional[str]:
    """分型矩阵。drive 缺失时返回 None（无内外盘不做分型，不猜）。"""
    if drive is None or vgear is None:
        return None
    down = change_pct is not None and float(change_pct) < 0
    expanding = vgear in ("温和放量", "明显放量", VOLUME_EXTREME_GEAR)
    if down and drive > 0:
        if vgear == VOLUME_EXTREME_GEAR and position == "高位":
            return "对倒诱多嫌疑"
        if expanding and position in ("低位", "中继"):
            return "资金接货型"
        if not expanding:
            return "抛压衰竭型"
        # 跌+外盘占优+放量+破位/高位以外：高位已先行排除，破位时外盘占优
        # 与趋势矛盾更可能是出逃中的对倒，按真实抛压处置（宁可错杀）。
        return "真实抛压型" if position == "破位" else "资金接货型"
    if down:  # drive <= 0
        return "真实抛压型" if expanding else "阴跌不止型"
    if drive < 0:
        return "承接型上涨"
    if vgear == VOLUME_EXTREME_GEAR and position == "高位":
        return "高位滞涨派发"
    return "健康推进型"


def star_rating(pattern: str, tgear: Optional[str], drive: Optional[float],
                vgear: Optional[str]) -> int:
    """分型可信度 1~3 星。星表达"这个分型有多可信"，不是涨跌方向。"""
    if pattern == "抛压衰竭型":
        # 恐慌区有真实换手（活跃及以上）→ 空头动能释放充分的证据更硬
        return 3 if tgear in ("活跃", "过热", "决战") else 2
    if pattern == "资金接货型":
        return 3 if (drive is not None and drive >= 0.05
                     and tgear in ("活跃", "过热", "决战")) else 2
    if pattern == "健康推进型":
        return 3 if vgear in ("温和放量", "明显放量") else 2
    if pattern == "真实抛压型":
        return 3
    if pattern == "高位滞涨派发":
        return 3
    return 2  # 对倒诱多嫌疑 / 阴跌不止型 / 承接型上涨


def _format_amount(amount: float) -> str:
    """资金净额（元）→ 亿/万 文案（与模板 _signed_amount 同口径，避免循环导入）。"""
    direction = "流出" if amount < 0 else "流入"
    abs_amt = abs(amount)
    if abs_amt >= 100_000_000:
        return f"{direction}{abs_amt / 100_000_000:.2f}亿"
    if abs_amt >= 10_000:
        return f"{direction}{abs_amt / 10000:.0f}万"
    return f"{direction}{abs_amt:.0f}元"


def main_force_net3(inst: Optional[Dict]) -> Optional[float]:
    """④资金票里主力源的近3日净额（元）。fund_flow_5d 是按日净流入序列。"""
    if not isinstance(inst, dict):
        return None
    try:
        raw = ((inst.get("votes") or {}).get("main_force") or {}).get("raw") or {}
        points = raw.get("fund_flow_5d") or []
        values = [float(p.get("value")) for p in points[-3:] if p.get("value") is not None]
    except (TypeError, ValueError, AttributeError):
        return None
    return sum(values) if values else None


def main_force_weight(inst: Optional[Dict]) -> Optional[float]:
    if not isinstance(inst, dict):
        return None
    weights = inst.get("effective_vote_weights") or inst.get("vote_weights") or {}
    w = weights.get("main_force") if isinstance(weights, dict) else None
    try:
        return float(w) if w is not None else None
    except (TypeError, ValueError):
        return None


def vote_score(inst: Optional[Dict]) -> Optional[int]:
    if not isinstance(inst, dict):
        return None
    try:
        return int(inst.get("vote_score", 0) or 0)
    except (TypeError, ValueError):
        return None


def detect_conflict(pattern: str, inst: Optional[Dict]) -> Optional[str]:
    """冲突检测（3.3）。返回 ⚠ 行文案或 None。"""
    if not isinstance(inst, dict):
        return None
    score = vote_score(inst)
    net3 = main_force_net3(inst)
    if pattern in _BULLISH_PATTERNS:
        vs_bits = []
        if score is not None and score < 0:
            vs_bits.append(f"资金投票{score:+d}票")
        if net3 is not None and net3 < 0:
            w = main_force_weight(inst)
            w_text = f"(权重{w:g})" if w is not None else ""
            vs_bits.append(f"主力3日净{_format_amount(net3)}{w_text}")
        if vs_bits:
            return (
                f"⚠量价资金背离：量能信号偏多 vs {'、'.join(vs_bits)}，"
                "主动接货疑似散单，降一级观察"
            )
        return None
    if pattern in _BEARISH_PATTERNS:
        if score is not None and score > 0:
            return f"⚠资金逆势：资金投票{score:+d}票 vs 量能信号偏空，次日验证优先"
    return None


def commission_cross_check(drive: Optional[float], wb_pct) -> Optional[str]:
    """委比交叉验证（3.4）。委比缺失或噪音区间返回 None。"""
    if drive is None or wb_pct is None:
        return None
    try:
        wb = float(wb_pct)
    except (TypeError, ValueError):
        return None
    if wb != wb or abs(wb) < WB_SIGNIFICANT_PCT:
        return None
    same = (wb > 0) == (drive > 0)
    if same:
        return "委托一致(委比与主动差同向)"
    if drive < 0:
        return "⚠委托背离：内盘占优+委比为正，托价嫌疑"
    return "⚠委托背离：外盘占优+委比为负，压单吸筹或边拉边撤"


def _limit_threshold_pct(stock_code: str, stock_name: str) -> float:
    """板块涨跌幅限制（%）：主板10 / 创业板·科创板20 / 北交所30 / ST 5。"""
    code = str(stock_code or "")
    name = str(stock_name or "")
    if "ST" in name:
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code[:2] in ("43", "83", "87", "92") or code.startswith("8"):
        return 30.0
    return 10.0


def limit_exempt(change_pct, stock_code="", stock_name="") -> Optional[str]:
    """失效豁免（3.5）：涨跌停封板附近内外盘失真，跳过分型。

    封板代理按板块限价留 0.2% 边距——创业板 +15% 未封板不得误豁免。
    开板时段/集合竞价瞬态的精确判定需逐笔数据，二期接入。
    """
    try:
        chg = float(change_pct)
    except (TypeError, ValueError):
        return None
    threshold = _limit_threshold_pct(stock_code, stock_name)
    if abs(chg) >= threshold - LIMIT_PROXY_MARGIN:
        return f"涨跌停附近({chg:+.1f}%，{threshold:.0f}%限价板)，封板内外盘失真，豁免分型"
    return None


def kline_position_inputs(kline, close=None) -> Dict[str, Optional[float]]:
    """从 K 线派生位置档输入：近20日涨幅 gain_20d、MA20 斜率 ma20_falling。

    供 orchestrator 组装推送数据时透传（推送字典不带整段 K 线，避免落库膨胀）。
    """
    closes: List[float] = []
    for k in kline or []:
        if not isinstance(k, dict):
            continue
        try:
            c = float(k.get("收盘", k.get("close", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if c > 0:
            closes.append(c)
    out: Dict[str, Optional[float]] = {"gain_20d": None, "ma20_falling": None}
    if len(closes) >= 21 and closes[-1] > 0:
        out["gain_20d"] = closes[-1] / closes[-21] - 1
    if len(closes) >= 25:
        ma20 = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-25:-5]) / 20
        out["ma20_falling"] = ma20 < ma20_prev
    return out


def _near_high_pct(close, prior_high, recent_high) -> Optional[float]:
    high = None
    for candidate in (prior_high, recent_high):
        try:
            value = float(candidate or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            high = value
            break
    try:
        price = float(close or 0)
    except (TypeError, ValueError):
        return None
    if not high or not price:
        return None
    return (high - price) / high


def _judgment_text(change_pct, vgear, drive, rsi6) -> str:
    """判读行：涨跌方向 + 量能 + 主动差 + RSI6 → 组合复述（有则拼，无则略）。"""
    bits = []
    try:
        chg = float(change_pct)
        direction = "跌" if chg < 0 else ("涨" if chg > 0 else "平")
        bits.append(f"{direction}{abs(chg):.1f}%")
    except (TypeError, ValueError):
        pass
    if vgear == "缩量":
        bits.append("量能萎缩")
    elif vgear == "平量":
        bits.append("量能持平")
    elif vgear:
        bits.append(vgear)
    if drive is not None and drive > 0:
        bits.append("主动买盘未失守")
    elif drive is not None and drive < 0:
        bits.append("主动卖压占优")
    if rsi6 is not None:
        try:
            r6 = float(rsi6)
            if r6 <= 25:
                bits.append(f"RSI6={r6:.0f}超卖")
            elif r6 >= 85:
                bits.append(f"RSI6={r6:.0f}超买")
            else:
                bits.append(f"RSI6={r6:.0f}")
        except (TypeError, ValueError):
            pass
    return "、".join(bits)


def build_volume_pattern(data: Dict) -> Dict:
    """从推送数据字典构建分型结果。纯函数，无 I/O，字段缺失逐级降级。

    data 需要的字段（缺失可容忍，但缺内外盘不做分型）：
      tech_signals.order_flow / volume_snapshot / rsi6
      volume_ratio, turnover_rate, change_pct, current_price, ma5/10/20
      prior_high / recent_high（三重门口径前高）
      gain_20d / ma20_falling（kline_position_inputs 派生）
      institutional_holding（冲突检测）
    """
    tech = data.get("tech_signals") or {}
    plan = data.get("execution_plan") or {}
    vs = plan.get("volume_snapshot") or tech.get("volume_snapshot") or {}

    vr = vs.get("volume_ratio")
    if vr is None:
        vr = data.get("volume_ratio")
    turnover = vs.get("turnover_rate")
    if turnover is None:
        turnover = data.get("turnover_rate")
    # 个股自身分位过热(P90)与绝对换手档互补，分型行保留该警示
    turnover_hot = bool(vs.get("turnover_hot"))

    flow = tech.get("order_flow") or {}
    drive = active_drive(flow)
    wb = flow.get("wb_ratio")

    vgear = volume_gear(vr)
    tgear = turnover_gear(turnover)
    close = data.get("current_price")
    near_high = _near_high_pct(close, data.get("prior_high"), data.get("recent_high"))
    position = position_gear(
        close, data.get("ma5"), data.get("ma10"), data.get("ma20"),
        near_high, data.get("gain_20d"), data.get("ma20_falling"),
    )

    result = {
        "available": True,
        "exempt": False, "exempt_reason": "",
        "change_pct": data.get("change_pct"),
        "volume_ratio": float(vr) if vr is not None else None,
        "vgear": vgear,
        "turnover": float(turnover) if turnover is not None else None,
        "tgear": tgear,
        "turnover_hot": turnover_hot,
        "drive": drive, "drive_label": drive_label(drive),
        "wb": wb,
        "position": position, "near_high_pct": near_high,
        "gain_20d": data.get("gain_20d"),
        "pattern": None, "star": 0, "verdict": "", "bias": 0,
        "strategy": "", "strategy_short": "",
        "conflict": None, "commission_note": None,
        "judgment": "", "confirms": [], "confirm_target": "",
        "summary_short": "",
    }

    exempt_reason = limit_exempt(
        data.get("change_pct"),
        stock_code=str(data.get("stock_code") or ""),
        stock_name=str(data.get("stock_name") or ""),
    )
    if exempt_reason:
        result["exempt"] = True
        result["exempt_reason"] = exempt_reason
        result["summary_short"] = "涨跌停豁免"
        return result

    inst = data.get("institutional_holding")
    rsi6 = data.get("rsi6") or tech.get("rsi6")
    pattern = classify(data.get("change_pct"), drive, vgear, position)
    result["judgment"] = _judgment_text(data.get("change_pct"), vgear, drive, rsi6)
    if pattern is None:
        # 内外盘/量比缺失：不做分型，量能档与位置档照常展示（诚实降级）
        result["summary_short"] = "盘口数据不足"
        return result

    defs = _PATTERN_DEFS[pattern]
    result.update({
        "pattern": pattern,
        "short": defs["short"],
        "star": star_rating(pattern, tgear, drive, vgear),
        "verdict": defs["verdict"],
        "bias": defs["bias"],
        "strategy": defs["strategy"],
        "strategy_short": defs["strategy_short"],
        "confirms": list(defs["confirms"]),
        "confirm_target": defs["confirm_target"],
        "conflict": detect_conflict(pattern, inst),
        "commission_note": commission_cross_check(drive, wb),
        "summary_short": f"{defs['short']}·{defs['strategy_short']}",
    })
    if result["conflict"]:
        result["summary_short"] = f"⚠{defs['short']}·降一级"
    return result


def _star_text(star: int) -> str:
    return "★" * max(0, min(3, star)) + "☆" * (3 - max(0, min(3, star)))


def render_volume_pattern(result: Dict) -> List[str]:
    """渲染③量能块（紧凑买入卡/观察卡共用）。返回纯文本行列表。"""
    if not result or not result.get("available"):
        return []
    lines = []
    if result.get("exempt"):
        vr = result.get("volume_ratio")
        head = f"③量能 [豁免] 量比{vr:.2f}" if vr is not None else "③量能 [豁免]"
        lines.append(head)
        lines.append(f"③·豁免: {result['exempt_reason']}")
        return lines

    vr = result.get("volume_ratio")
    t = result.get("turnover")
    d = result.get("drive")
    data_bits = []
    if vr is not None:
        data_bits.append(f"量比{vr:.2f}({result.get('vgear') or '?'})")
    else:
        data_bits.append("量比:N/A")
    if t is not None:
        hot = "⚠️>P90过热" if result.get("turnover_hot") else ""
        data_bits.append(f"换手{t:.2f}%({result.get('tgear') or '?'}){hot}")
    else:
        data_bits.append("换手:N/A")
    if d is not None:
        data_bits.append(f"主动差{d * 100:+.1f}%({result['drive_label']})")
    else:
        data_bits.append("内外盘:N/A")
    data_bits.append(f"位置:{result['position']}")

    pattern = result.get("pattern")
    if pattern:
        lines.append(f"③量能 [{pattern}] {_star_text(result['star'])}")
    else:
        lines.append(f"③量能 [盘口数据不足] {' | '.join(data_bits)}")
        return lines
    lines.append("③·数据: " + " | ".join(data_bits))
    if result.get("judgment"):
        lines.append(f"③·判读: {result['judgment']}——{result['verdict']}")
    if result.get("conflict"):
        lines.append(f"③·{result['conflict']}")
    if result.get("commission_note"):
        lines.append(f"③·{result['commission_note']}")
    if result.get("confirms"):
        lines.append(
            f"③·确认: {' | '.join(result['confirms'])} → {result['confirm_target']}"
        )
    return lines


def pattern_summary_line(data: Dict) -> str:
    """报告级"量能判定一览"的单票片段：名称[分型·策略]。

    数据不足返回空串（一览行不放假结论）。
    """
    result = build_volume_pattern(data or {})
    if not result.get("available"):
        return ""
    name = str((data or {}).get("stock_name") or "").strip()
    short = result.get("summary_short") or ""
    if not name or not short:
        return ""
    return f"{name}[{short}]"
