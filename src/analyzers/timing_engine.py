"""
择时引擎 - 第四层（参数化重构版）

所有判断阈值已抽到 config/timing.yaml，回测与实盘共用同一份配置。
两种进场（恐慌抄底左侧 / 确认追强右侧）+ 四类出场（冲高止盈/破位止损/预期兑现/主动认错）
套利低吸已移至 position_builder.py 作为加仓信号
止损价自动计算

【参数化重构要点】
1. 所有硬编码阈值 → self._tc[...] 读取 timing.yaml
2. 新增 backtest_mode：支持回测时注入历史 K 线，保证回测逻辑 = 实盘逻辑
3. 修复 3 个 bug：
   - stop_loss_multiplier 配置已存在但未接入（行 883 原代码用裸 0.97）
   - _tech_cache_weekly 从未初始化（导致周线 MACD 永远 False）
   - buffer 计算后未使用（半成品，现接入为可选止损缓冲）
"""
import logging
import threading
_thread_local = threading.local()
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config_models import load_config
from ..analyzers.market_env import get_market_environment  # 修复 BUG-T1: D1 充分条件依赖此函数，原代码未导入导致恐慌抄底永不触发
from ..data_layer.akshare_adapter import get_akshare_adapter
from ..data_layer.skill_wrapper import get_skill_wrapper
from ..data_layer.stock_data import _volume_share_factor
from ..analyzers.stock_filter import get_stock_filter, FilterResult
from .signal_rejection import RejectionLedger

logger = logging.getLogger(__name__)


@dataclass
class EntrySignal:
    """入场信号"""
    stock_code: str
    stock_name: str
    entry_type: str               # 恐慌抄底 / 套利低吸 / 确认追强 / 价量突破
    entry_trigger_price: float
    stop_loss: float               # 系统自动计算
    target_type: str               # 主升持有 / 冲高止盈 / 持有观察
    target_range: List[float] = field(default_factory=list)  # 止盈区间
    position_level: str = "normal"  # normal / heavy
    applicable_modes: List[str] = field(default_factory=list)  # 适用操作模式
    sector_status: str = ""        # 板块状态
    sector_name: str = ""         # 板块名称
    sw_level2: str = ""           # 同花顺行业（原名申万二级，字段名保留兼容）
    sw_level3: str = ""           # 行业三级（字段名保留兼容）
    trigger_reason: str = ""       # 触发原因描述
    strategy_summary: str = ""     # 策略逻辑透传
    confidence: str = "中"         # 信号置信度
    benchmark_price: float = 0.0   # RRR/仓位唯一基准，禁止与现价混用
    rrr_low: Optional[float] = None
    rrr_high: Optional[float] = None
    kline_patterns: List[Dict] = field(default_factory=list)
    tech_data: Dict = field(default_factory=dict)
    execution_plan: Dict = field(default_factory=dict)
    # 【一】可证伪假说（X/Y/Z/W 四要素，出厂检查已通过才到达这里）
    hypothesis: Dict = field(default_factory=dict)
    # 【三】信号事件 ID（生命周期链接，回执后转 triggered）
    event_id: str = ""
    # 【三】受众：empty=仅空仓者成立 / holding=持仓者加仓参考
    audience: str = "empty"
    # 【二】基本面摘要（业绩雷/盈利质量/财报窗口，来自 fundamental_gate）
    fundamental_note: str = ""
    # 【Phase3】估值透镜摘要（估值泡沫/低基数反转/亏损分型/资金-分析师冲突）
    valuation_note: str = ""
    # 【Phase4 P1-1】防守模式降仓放行标记（三重门证据 + 外推量能）
    defensive_chase: Optional[Dict] = None
    # 事件出生时落审计：Y 不再只给结果，也给公式和输入。
    entry_y_formula: str = ""
    entry_y_inputs: Dict = field(default_factory=dict)


@dataclass
class ExitSignal:
    """出场信号"""
    stock_code: str
    stock_name: str
    exit_type: str                 # 破位止损 / 破位预警 / 冲高止盈 / MA5压制 / 技术走弱 / 板块退潮 / 策略兑现 / 信号作废 / 信号过期
    trigger_price: float
    stop_loss_price: float
    reason: str
    urgency: str = "重要"
    mode_constrained: bool = False
    sector_status: str = ""
    sector_name: str = ""
    sw_level3: str = ""
    tech_data: Dict = field(default_factory=dict)
    # 【四】配对出场标记：paired=策略原生 Z/W 硬触发 / system=旧系统兜底（降观察）
    source: str = "system"
    paired_strategy: str = ""
    # 只读引用事件库；卖出信号不得自行描述买入事件的阶段。
    event_id: str = ""

@dataclass
class StopLossCalc:
    """止损价计算结果"""
    stock_code: str
    current_price: float
    support_candidates: List[Dict[str, float]]
    chosen_support: float
    stop_loss_price: float
    resistance: float
    ladder: list = None  # C4: 阶梯止损 [{support, price, reduce_ratio}, ...]


# ============================================================
# 默认阈值（当 timing.yaml 缺失时使用，保证向后兼容）
# ============================================================

DEFAULT_TIMING_CONFIG = {
    "panic_bottom": {
        "index_drop_threshold": 4.0,
        "gem_star_drop_threshold": 5.0,
        "ad_ratio_extreme_weak": 0.15,
        "panic_volume_ratio": 2.0,
        "panic_volume_index_drop": 2.0,
        "significant_volume_ratio": 1.5,
        "stock_drop_threshold": -7,        # 单日暴跌 > 7%（从-5%提高）
        "drop_5d_threshold": -0.15,        # 5日跌幅 > 15%
        "high_confidence_market_conds": 1,
        "high_confidence_stock_conds": 2,
    },
    "arbitrage": {
        "shrinking_volume_ratio": 1.0,
        "min_trigger_conditions": 2,
    },
    "momentum_chase": {
        "ma20_trend_min_klines": 21,
        "ma20_window": 20,
        "breakout_price_ratio": 0.99,
        "volume_confirm_ratio": 1.2,
    },
    "volume_breakout": {
        "require_rs_above_ma": True,
        "require_event_boundary": True,   # 【三】事件边界：昨收在昨日MA25下方且今价站上MA25才诞生
        "require_bullish_close": True,    # 【三】诞生条件：当日收阳（两条腿缺一不可）
    },
    # 【Phase4 P0-1】量能外推口径：盘中累计量 ÷ U型分位 → 全天量
    # 蘅东光 9/7 实证：1.02x 拦截 → 外推 2.0x 触发。10:00 前禁用、封板禁用。
    "volume_projection": {
        "enabled": True,
        "start_time": "10:00",
        "end_time": "14:45",
        "breakout_threshold": 1.2,        # 外推口径量能突破阈值（比实际口径 1.0 更严）
        "error_alert_pct": 15.0,          # 作废条件：10:30 后误差持续超此值 → 换标的池分位表
        "error_check_days": 10,
    },
    # 【Phase4 P1-1】防守模式确认追强降仓可用（三重门 + 风险预算仓位）
    "defensive_chase": {
        "enabled": True,                   # 关闭 = 回退“防守禁追强”（磨空成本继续留档）
        "risk_budget_pct": 0.01,          # 单笔风险预算（账户%）
        "max_probe_ratio": 0.3333,        # 试探仓封顶（中波动近似 1/3）
        "min_stop_distance_pct": 0.01,    # 防毫厘止损杠杆化
        "rsi_overheat": 67.0,             # 门一时机：RSI14 ≥ 此值视为过热（蘅东光 9/3 RSI66.6 类）
        "new_high_ratio": 0.99,           # 门一①：现价 ≥ 近20日高×此值
        "volume_ratio_min": 1.2,          # 门一②：量比下限
        "projected_volume_min": 1.2,      # 门一③：外推量能下限（与实际口径取其一）
        "adx_min": 25.0,                  # 门一④：单边力度（ADX，蘅东光 9/7 实证 48）
        "profit_yoy_min": 30.0,           # 门二：净利同比下限（%），且非低基数/业绩雷/亏损分型
        "shareholder_dispersion_max": 0.20,  # 门二：户数增加 >20% = 筹码分散/减持迹象 → 拦
    },
    # 【Phase4 P1-2】再入场循环：止损是尝试的结束，不是死刑判决
    "reentry": {
        "enabled": True,
        "max_reentries": 2,               # 同标的再入场次数上限（超过 = 真死刑）
        "new_high_ratio": 0.99,           # 创新高再入场：现价 ≥ 近20日高×此值
    },
    # 【Phase4 P3-1】趋势延续：突破后 1~10 日延续段回踩入场（沃尔德 9/7 类）
    "trend_continuation": {
        "enabled": True,
        "min_bars_since_breakout": 1,     # 突破后第 N 日起可入场（当日归价量突破）
        "max_bars_since_breakout": 10,    # 超过 N 日视为趋势尾段，不再入场
        "volume_ratio_min": 1.2,          # 回踩后再放量确认
        "ma_alignment": True,             # 要求 MA 多头排列
    },
    "confidence": {
        "rrr_quality_threshold": 2.5,     # 【P0-2】RRR≥此值 +1 票（隐含胜率 28.6%）
        "rrr_quality_cap": 5.0,           # 【Phase5】RRR>此值不加分（分母过小，神话数字嫌疑）
    },
    # 【Phase5 回炉】风险乘数波动率分档（验收实证：精智达振幅8.16%拿风险1.00，
    # 博杰反而 0.60——风险系数必须规则化，不能随机出现）
    "risk": {
        "volatility_tier": {
            "enabled": True,
            "high_amp": 0.08,              # 振幅≥8% → 0.6（精智达 9/8 实证）
            "mid_amp": 0.05,               # 振幅≥5% → 0.8
            "high_mult": 0.6,
            "mid_mult": 0.8,
            "window": 5,                   # 近 N 日平均振幅窗口
        },
    },
    "hypothesis_gate": {
        "enabled": True,                  # 【一】可证伪性出厂检查总开关
        # 【P0-2 + Phase5 回炉】Z 线默认 buffered_structure：结构位−clamped(k×ATR)
        # （决策记录“结构位或结构位加 1~2 倍 ATR 缓冲”的后半句；
        # 精智达 9/8 实证：裸结构位距买点 0.84%，日内噪声十分之一即可扫损）
        "z_line_mode": "buffered_structure",
        "z_atr_mult": 1.5,               # 高波动档（ATR/结构位≥5%）的缓冲倍数
        "z_atr_mult_low_vol": 2.0,       # 低波动档（ATR 绝对值小，倍数补足）
        "z_high_vol_ratio": 0.05,        # 高/低波动分档线（ATR/结构位）
        "z_pct_buffer": 0.005,           # 缓冲下限（结构位%，防毫厘止损）
        "z_buffer_max_pct": 0.08,        # 缓冲上限（结构位%，防拖到跌停价——博杰旧病）
        "bare_noise_min_pct": 0.008,     # 裸结构位防噪下限（止损距离<0.8%拒绝出厂）
        "min_z_buffer_pct": 0.01,        # ATR 缺失时 Y-Z 最小百分比宽度
    },
    "data_guard": {
        "turnover_threshold_pct": 10.0,   # 【二】换手/量级一致性：换手阈值（%）
        "max_volume_shares": 1.0e9,       # 【二】换手<阈值且量>此值 → 脏数据拦截
    },
    "signal_lifecycle": {
        "valid_days": 5,                  # 【三】回踩买点有效期（N日）
    },
    "strategy_stats": {
        "min_trades_for_stats": 30,       # 【六】分层统计最小样本
        "kill_rolling_window": 50,        # 【六】滚动窗口
        "kill_min_trades": 50,            # 【六】下线判定的最小样本
    },
    "exit": {
        "breakdown": {
            "volume_confirm_ratio": 0.8,
            "heavy_volume_ratio": 1.3,
        },
        "exhaustion": {
            "rsi_severe_overbought": 80,
            "rsi_overbought": 70,
            "rsi_divergence_window": 14,
            "rsi_divergence_threshold": 65,
            "volume_price_div_window": 10,
            "volume_price_div_avg_window": 5,
            "volume_shrink_ratio": 0.8,
            "upper_shadow_window": 3,
            "upper_shadow_strong_ratio": 1.5,
            "upper_shadow_medium_ratio": 1.0,
            "upper_shadow_min_count": 2,
            "long_upper_shadow_body_ratio": 3,
            "long_upper_shadow_volume_ratio": 1.5,
            "upper_shadow_body_ratio_medium": 2,
            "kdj_dead_cross_k": 70,
            "kdj_blunt_j": 100,
            "macd_kdj_resonance_k": 60,
            "vote_neutral_negative_score": 0,
            "ma5_bias_overheat": 0.08,
            "strong_signal_min_count": 1,
            "macd_as_exhaustion": False,  # MACD 死叉不作为独立衰竭信号（已在投票系统算过）
        },
    },
    "stop_loss": {
        "multiplier": 0.97,
        # 修复(2026-08-27): 默认表与 config/timing.yaml 漂移——yaml 在 C1 整改已定为
        # 0.95/0.06，但本表仍留 BUG-E1 时期的 0.92/0.12，yaml 缺失时降级行为会倒退。
        # 现对齐 yaml 真值（fallback 路径另有 ATR 自适应，见 calculate_stop_loss）。
        "fallback_support_ratio": 0.95,
        "max_support_distance": 0.06,
        "use_atr_buffer": False,
        "atr_min_klines": 15,
        "atr_period": 14,
        "atr_multiplier": 2,
        "fallback_buffer_ratio": 0.02,
    },
    "derivation": {
        "vote_bullish_threshold": 1.0,
        "vote_bearish_threshold": -1.0,
        "rsi_high_contradiction": 65,
        "rsi_low_contradiction": 35,
    },
    "tech_data": {
        "min_klines_for_indicator": 20,
        "min_klines_for_tech_vote": 30,
        "ma25_window": 25,
        "ma25_prev_window": 26,
        "ma60_window": 60,
        "ma120_window": 120,
        "volume_ma60_window": 60,
        "recent_extreme_window": 20,
        "volume_ratio_avg_window": 6,  # 6 条算、剔除当日 = 前5日均量；标准量比收盘时=当日量/前5日均量
        "shrinking_volume_ratio": 1.0,  # 设计问题5: 从 0.8 放宽到 1.0
        "pullback_ma5_bias_tolerance": 0.03,  # 设计问题5: 从 0.01 放宽到 0.03
        "pullback_ma10_bias_tolerance": 0.03,
        "hammer_lower_shadow_ratio": 2,
        "limit_down_ratio": 0.9,
        "limit_down_touch_tolerance": 1.002,
        "limit_down_open_threshold": 1.02,
        "weekly_macd_min_weeks": 26,
        "ema12_period": 12,
        "ema26_period": 26,
        "dea_smooth_iterations": 8,
    },
    "target_range": {
        "panic_bottom_low": 1.08,
        "panic_bottom_high": 1.18,
        "arbitrage_with_resistance_low": 0.97,
        "arbitrage_with_resistance_high": 1.02,
        "arbitrage_no_resistance_low": 1.05,
        "arbitrage_no_resistance_high": 1.08,
        "momentum_chase_low": 1.05,
        "momentum_chase_high": 1.12,
        "volume_breakout_low": 1.08,
        "volume_breakout_high": 1.15,
        "default_with_resistance_low": 0.97,
        "default_with_resistance_high": 1.02,
        "default_no_resistance_low": 1.05,
        "default_no_resistance_high": 1.08,
    },
    "prefetch": {
        "index_min_klines": 2,
        "market_vol_avg_window": 20,
        "max_workers": 4,
    },
    "backtest": {
        "budget_per_stock": 250000,
        "max_concurrent_positions": 4,
        "min_hold_days": 3,
        "max_hold_days": 10,
        "cooldown_days_after_sell": 3,
        "take_profit_threshold": 0.08,
        "ma5_pressure_threshold": 0.03,
        "confess_wrong_days": 3,
        "confess_wrong_pnl_threshold": 0.02,
    },
}


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


class TimingEngine:
    """择时引擎（参数化重构版）"""

    def __init__(self, backtest_mode: bool = False, params_override: Optional[dict] = None):
        """
        Args:
            backtest_mode: True=回测模式（注入历史K线），False=实盘模式（拉实时数据）
            params_override: 参数覆盖（合并到 timing.yaml 配置上，用于网格搜索）
        """
        # 加载 timing.yaml 配置
        try:
            raw = load_config("timing.yaml").get("timing", {})
        except Exception as e:
            logger.warning("timing.yaml 加载失败，使用默认配置: %s", e)
            raw = {}

        # 合并：默认 < yaml < override
        self._tc = _deep_merge(DEFAULT_TIMING_CONFIG, raw)
        if params_override:
            self._tc = _deep_merge(self._tc, params_override)

        # stop_loss 配置统一从 timing.yaml stop_loss.multiplier 读取
        self._risk_config = {}
        try:
            self._risk_config = load_config("risk.yaml").get("risk", {})
        except Exception as e:
            logger.debug("非关键异常: %s", e)

        self._akshare = get_akshare_adapter()
        self._skill = get_skill_wrapper()
        self._stock_filter = get_stock_filter()
        self._tech_cache: Dict[str, Dict] = {}
        self._tech_cache_weekly: Dict[str, List[Dict]] = {}  # 修复 bug: 原代码从未初始化
        self._tech_data_full: Dict[str, Dict] = {}  # 完整 tech_data 缓存（含技术指标+机构打分），供观察列表复用
        self._exit_diagnostics: Dict[str, str] = {}
        self._market_cache: Optional[Dict] = None
        self._cache_lock = threading.Lock()

        # 【三】信号生命周期（状态→事件）：实盘用 DB 跨日去重，回测/无DB用内存
        from .signal_lifecycle import SignalLifecycle, InMemorySignalEventStore
        if backtest_mode:
            store = InMemorySignalEventStore()
        else:
            try:
                from .signal_lifecycle import DbSignalEventStore
                store = DbSignalEventStore()
            except Exception as e:
                logger.warning("DB事件存储不可用，退化为内存生命周期: %s", e)
                store = InMemorySignalEventStore()
        self._lifecycle = SignalLifecycle(
            store, valid_days=int(self._cfg("signal_lifecycle", "valid_days", default=5))
        )
        # 【一】出厂拒绝留痕（供 unified_engine 采集 → signal_rejections 表）
        self._rejection_ledger = RejectionLedger()


        # 回测模式上下文
        self._backtest_mode = backtest_mode
        self._backtest_kline: Dict[str, List[Dict]] = {}
        self._backtest_index_kline: List[Dict] = []
        self._backtest_date: str = ""

    # ============================================================
    # 配置访问辅助方法
    # ============================================================

    @property
    def _entry_rejections(self) -> Dict[str, Dict]:
        """向后兼容：读取被拒暂存字典（测试/采集方直接访问）。"""
        return self._rejection_ledger.pending

    def _cfg(self, *path, default=None):
        """读取嵌套配置，如 self._cfg("panic_bottom", "index_drop_threshold")"""
        node = self._tc
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ============================================================
    # 回测模式：注入历史 K 线
    # ============================================================

    def set_backtest_context(
        self,
        date: str,
        kline_data: Dict[str, List[Dict]],
        index_kline: List[Dict],
    ):
        """
        设置回测上下文（每个回测日调用一次）

        Args:
            date: 当前回测日期 YYYY-MM-DD
            kline_data: {code: kline_sliced_to_date} 已切片到当前日期
            index_kline: 上证指数 K 线，已切片到当前日期
        """
        self._backtest_date = date
        self._backtest_kline = kline_data
        self._backtest_index_kline = index_kline
        # 重置缓存，强制用新数据重算
        with self._cache_lock:
            self._tech_cache.clear()
            self._tech_cache_weekly.clear()
            self._tech_data_full.clear()
            self._exit_diagnostics.clear()
            self._market_cache = None

    def reset_caches(self):
        """重置本轮缓存（每次 run_unified_analysis 前调用）"""
        with self._cache_lock:
            self._tech_cache.clear()
            self._tech_cache_weekly.clear()
            self._tech_data_full.clear()
            self._exit_diagnostics.clear()
            self._market_cache = None
            self._rejection_ledger.clear()

    # ============================================================
    # 大盘数据预取
    # ============================================================

    def prefetch_market_data(self):
        """预取大盘数据（每轮只调一次，结果缓存到 _market_cache）"""
        if self._market_cache is not None:
            return
        data = {}

        # 回测模式：从注入的指数 K 线计算
        if self._backtest_mode and self._backtest_index_kline:
            self._prefetch_market_from_backtest(data)
            self._market_cache = data
            return

        # 实盘模式：从 akshare 拉取
        index_result = None
        index_min_klines = self._cfg("prefetch", "index_min_klines", default=2)
        market_vol_window = self._cfg("prefetch", "market_vol_avg_window", default=20)

        # 上证指数日内跌幅
        try:
            index_result = self._akshare.get_index_data("000001")
            if index_result.success and index_result.data and len(index_result.data) >= index_min_klines:
                idx_records = index_result.data
                idx_closes = [float(r.get("收盘", r.get("close", 0))) for r in idx_records]
                if len(idx_closes) >= 2 and idx_closes[-2] > 0:
                    data["index_daily_drop"] = round((idx_closes[-1] - idx_closes[-2]) / idx_closes[-2] * 100, 2)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 全市场成交额
        try:
            vol_result = self._akshare.get_market_volume()
            if vol_result.success and vol_result.data:
                data["market_volume_yi"] = vol_result.data.get("total_volume_yi", 0)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 全市场20日均成交额
        try:
            if index_result and index_result.success and index_result.data and len(index_result.data) >= market_vol_window:
                idx_records = index_result.data
                vol_20d = [float(r.get("成交量", r.get("volume", 0))) for r in idx_records[-market_vol_window:]]
                if vol_20d and sum(vol_20d) > 0:
                    avg_sh_vol = sum(vol_20d) / len(vol_20d)
                    if data.get("market_volume_yi", 0) > 0 and vol_20d[-1] > 0:
                        ratio = data["market_volume_yi"] / vol_20d[-1]
                        data["market_volume_20d_avg"] = round(avg_sh_vol * ratio, 2)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 涨跌比
        try:
            ad_result = self._akshare.get_advance_decline()
            if ad_result.success and ad_result.data:
                data["advance_decline_ratio"] = ad_result.data.get("advance_decline_ratio", 1.0)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 缓存指数K线（供RS line计算）
        try:
            if index_result and index_result.success and index_result.data:
                data["index_kline"] = index_result.data
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 双创跌幅
        try:
            from ..analyzers.gem_sci_tech_scorer import get_gem_sci_tech_analysis
            gst = get_gem_sci_tech_analysis()
            gem = gst.get("gem") or {}
            star = gst.get("star") or {}
            gem_drop = abs(gem.get("change_pct", 0)) if gem.get("change_pct", 0) < 0 else 0
            star_drop = abs(star.get("change_pct", 0)) if star.get("change_pct", 0) < 0 else 0
            data["gem_sci_tech_drop"] = max(gem_drop, star_drop)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        self._market_cache = data
        logger.info("大盘数据预取完成: index_drop=%.2f, volume=%.0f亿, ad_ratio=%.2f",
                    data.get("index_daily_drop", 0),
                    data.get("market_volume_yi", 0),
                    data.get("advance_decline_ratio", 1.0))

    def _prefetch_market_from_backtest(self, data: Dict):
        """回测模式：从注入的指数 K 线计算大盘数据"""
        idx = self._backtest_index_kline
        if not idx or len(idx) < 2:
            return
        try:
            closes = [float(r.get("close", 0)) for r in idx]
            vols = [float(r.get("volume", 0)) for r in idx]
            if len(closes) >= 2 and closes[-2] > 0:
                data["index_daily_drop"] = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
            market_vol_window = self._cfg("prefetch", "market_vol_avg_window", default=20)
            if len(vols) >= market_vol_window:
                data["market_volume_yi"] = vols[-1] / 1e8 if vols[-1] > 1e6 else vols[-1]
                data["market_volume_20d_avg"] = sum(vols[-market_vol_window:]) / market_vol_window / 1e8 if vols[-1] > 1e6 else sum(vols[-market_vol_window:]) / market_vol_window
            # 涨跌比回测中无法获取，给中性值
            data["advance_decline_ratio"] = 1.0
            # 双创跌幅回测中无法获取
            data["gem_sci_tech_drop"] = 0
            # 缓存指数 K 线供 RS line 计算
            data["index_kline"] = idx
        except Exception as e:
            logger.debug("回测大盘数据计算失败: %s", e)

    def prefetch_hist_batch(self, codes: List[str], max_workers: Optional[int] = None):
        """并行预取个股K线（线程池），结果写入 _tech_cache"""
        if max_workers is None:
            max_workers = self._cfg("prefetch", "max_workers", default=4)
        # 回测模式：K 线已注入，跳过
        if self._backtest_mode:
            return
        new_codes = [c for c in codes if c and c not in self._tech_cache]
        if not new_codes:
            return
        logger.info("并行预取 %d 只个股K线 (workers=%d)", len(new_codes), max_workers)
        hist_calendar_days = self._cfg("prefetch", "hist_calendar_days", default=240)
        start_date = (datetime.now() - timedelta(days=hist_calendar_days)).strftime("%Y%m%d")
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for code in new_codes:
                futures[pool.submit(self._akshare.get_stock_hist, code, start_date=start_date)] = code
            for future in as_completed(futures):
                code = futures[future]
                try:
                    result = future.result()
                    if result.success and result.data:
                        with self._cache_lock:
                            self._tech_cache[code] = result.data
                except Exception as e:
                    logger.debug("预取K线失败 %s: %s", code, e)
        logger.info("并行预取完成: %d/%d 只命中", sum(1 for v in self._tech_cache.values() if v), len(new_codes))

    # ============ 入场信号 ============

    _ENTRY_PRIORITY = ["恐慌抄底", "套利低吸", "确认追强", "价量突破", "趋势延续"]

    def _merge_entry_signals(self, signals: List[EntrySignal], tech_data: Dict, market_mode: str, sector_status: str = "") -> List[EntrySignal]:
        """合并多条入场信号为一条综合信号，附带策略推导链。"""
        if not signals:
            return []

        triggered_types = list(dict.fromkeys(s.entry_type for s in signals))

        # 【Phase5 回炉】推导栏置信度优先用执行计划的真实置信度（含分数）：
        # 验收实证（9/8 精智达）——推导栏“置信度:高”（EntrySignal 硬编码）
        # 与置信度栏“2/6 中”（评分体系算出）矛盾，同一报告两口径。
        def _display_confidence(sig) -> str:
            plan = getattr(sig, "execution_plan", None)
            if isinstance(plan, dict) and plan.get("confidence"):
                cs = plan.get("confidence_score")
                total = plan.get("applicable_score")
                label = plan.get("confidence")
                if isinstance(cs, (int, float)) and isinstance(total, (int, float)) and total:
                    return f"{label}({int(cs)}/{int(total)})"
                return str(label)
            return getattr(sig, "confidence", "中")

        if len(signals) == 1:
            sig = signals[0]
            derivation = self._build_derivation(
                tech_data, market_mode,
                triggered_types=triggered_types,
                selected_type=sig.entry_type,
                confidence=_display_confidence(sig),
                signal_direction="entry",
                sector_status=sector_status,
            )
            sig.trigger_reason += "\n  推导: " + derivation
            return [sig]

        best_type = None
        for pt in self._ENTRY_PRIORITY:
            for s in signals:
                if s.entry_type == pt:
                    best_type = pt
                    break
            if best_type:
                break

        best = next(s for s in signals if s.entry_type == best_type)
        other_reasons = [s.trigger_reason for s in signals if s.entry_type != best_type and s.trigger_reason]

        best.trigger_reason = (
            str(best.trigger_reason)
            + ("（另满足：" + "；".join(other_reasons) + "）" if other_reasons else "")
        )

        derivation = self._build_derivation(
            tech_data, market_mode,
            triggered_types=triggered_types,
            selected_type=best_type,
            confidence=_display_confidence(best),
            signal_direction="entry",
            sector_status=sector_status,
        )
        best.trigger_reason += "\n  推导: " + derivation

        logger.info("入场信号合并: %s %d->1条（主类型=%s）", best.stock_code, len(signals), best_type)
        return [best]

    def _refresh_derivation_confidence(self, trigger_reason: str, plan) -> str:
        """推导栏置信度与置信度栏对齐（进第五轮的老问题根因）。

        推导在 build_execution_plan 之前生成（_merge_entry_signals 阶段），
        此时 sig.execution_plan 尚不存在，_display_confidence 只能回退到
        EntrySignal 硬编码的“高/中”——而置信度栏用评分体系的 0/6。
        Phase5 只改了 _display_confidence 的取值顺序，没解决时序：
        报告里仍是 推导栏“高/中” vs 置信度栏“0/6 低” 两口径并存。
        修复：执行计划建成后刷新 ④ 行的置信度口径。
        """
        import re
        cs = getattr(plan, "confidence_score", None)
        total = getattr(plan, "applicable_score", None)
        label = getattr(plan, "confidence", "低")
        try:
            if isinstance(cs, (int, float)) and isinstance(total, (int, float)) and total:
                conf_text = f"{label}({int(cs)}/{int(total)})"
            else:
                conf_text = str(label)
        except (TypeError, ValueError):
            conf_text = str(label)
        return re.sub(r"置信度:[^\n|]*", f"置信度:{conf_text}", str(trigger_reason))

    def check_entry_signals(
        self,
        stock_code: str,
        stock_name: str = "",
        market_mode: str = "defend",
        sector_status: str = "rotational",
        filter_result: Optional[FilterResult] = None,
        market_score: Optional[float] = None,
    ) -> List[EntrySignal]:
        """
        检查个股入场信号

        独立检查恐慌抄底/套利低吸/确认追强三种策略（它们可以共存），
        然后合并为一条综合信号，保留所有触发原因。
        每种标的每天最多返回 1 条信号。

        注意：filter_stock 由调用方（unified_engine）负责，这里不再重复调用。
        若 filter_result 未传入且非回测模式，才降级自行过滤（向后兼容）。
        """
        # 前置过滤：仅当调用方未传入 filter_result 时才自行过滤（避免重复调用）
        if not self._backtest_mode and filter_result is None:
            filter_result = self._stock_filter.filter_stock(stock_code, stock_name)
        if filter_result is not None and not filter_result.passed:
            logger.debug("%s 过滤未通过，跳过入场信号检查", stock_code)
            return []

        # C5: 记录当前 market_mode 供 _calculate_target_range 使用
        self._current_market_mode = market_mode

        # D5 板块状态只作为信号上下文；不再做入场侧全局闸门。
        # 退潮板块的执行约束在调度端处理：已持有标的禁止加仓，空仓标的仍按策略评估。

        # D6 黑名单机制（2026-07-25 整改）
        # 帖5/帖14：板块级pass清单 + 情绪性拉黑 + 拥挤主线回避
        # 黑名单可从 config/blacklist.yaml 加载，默认空
        # 注：check_entry_signals 签名无 sector_name，黑名单检查在调用方（unified_engine）做
        # 这里仅加载黑名单供 check_exit_signals 等其他方法使用
        blacklist_sectors = getattr(self, '_blacklist_sectors', None)
        if blacklist_sectors is None:
            try:
                import yaml
                from pathlib import Path
                bl_path = Path(__file__).parent.parent.parent / 'config' / 'blacklist.yaml'
                if bl_path.exists():
                    with open(bl_path, 'r', encoding='utf-8') as f:
                        bl = yaml.safe_load(f) or {}
                    blacklist_sectors = bl.get('sectors', [])
                else:
                    blacklist_sectors = []
            except Exception:
                blacklist_sectors = []
            self._blacklist_sectors = blacklist_sectors

        # 获取技术数据 + 止损价
        tech_data = self._fetch_tech_data(stock_code, market_mode)
        # 【P0-1】外推封板判定按代码的涨跌停幅度（北交所 30%/创科 20%/主板 10%）
        try:
            try:
                from ..loop.backtest_engine import get_limit_ratio
            except ImportError:
                from src.loop.backtest_engine import get_limit_ratio
            tech_data.setdefault("projection_limit_ratio", float(get_limit_ratio(stock_code)))
        except Exception:
            pass
        from .signal_plan import build_volume_snapshot
        data_guard = self._cfg("data_guard") or None
        volume_snapshot = build_volume_snapshot(
            tech_data, guard=data_guard, projection_config=self._projection_cfg(),
        )
        # 【P0-1】外推快照落误差记录表（收盘后回填实际值，作废条件的数据基础设施）
        if volume_snapshot.projected_volume_vs_ma60 is not None:
            try:
                from .volume_projection import record_projection
                record_projection(
                    stock_code=stock_code, stock_name=stock_name,
                    raw_ratio=volume_snapshot.volume_vs_ma60,
                    projected_ratio=volume_snapshot.projected_volume_vs_ma60,
                )
            except Exception as e:
                logger.debug("外推记录失败 %s: %s", stock_code, str(e)[:60])
        if volume_snapshot.dirty:
            tech_data["volume_data_valid"] = False
            tech_data["volume_snapshot"] = volume_snapshot.as_dict()
            tech_data["entry_blocked_reason"] = volume_snapshot.dirty_reason
            self._tech_data_full[stock_code] = tech_data
            return []
        if market_score is not None:
            tech_data["market_score"] = float(market_score)
        # 入场检查先落缓存，保证未触发买入时诊断用的是同一份完整数据
        self._tech_data_full[stock_code] = tech_data
        stop_loss_calc = self.calculate_stop_loss(stock_code, tech_data)

        # 【三】事件生命周期去重：同股同策略的活跃事件（valid）只诞生一次，
        # 不再原样重播（沃尔德连续两天推同一"站上MA25"的状态信号 → 事件化后消失）。
        # 活跃事件的演化路径（回踩有效/失效撤单/过期）由 evaluate_signal_events 处理。
        active_events = self._lifecycle.get_active_events(stock_code)
        active_types = {e.entry_type for e in active_events}
        if active_types:
            self._tech_data_full[stock_code] = tech_data
            logger.debug(
                "%s 存在活跃信号事件 %s，跳过重复生成（事件生命周期内）",
                stock_code, active_types,
            )
            return []

        # 【P1-2】再入场次数上限：止损是尝试的结束不是死刑判决，
        # 但同一标的再入场次数用尽（默认 2 次）后也不再重播。
        # 注意：再入场只由新事件驱动（创新高/收复Z线后策略条件重新满足），
        # 本检查只做“次数上限”，不携带任何沉没成本逻辑。
        try:
            from .signal_lifecycle import reentry_exhausted
            reentry_cfg = self._cfg("reentry") or {}
            if reentry_cfg.get("enabled", True) and reentry_exhausted(
                stock_code, int(reentry_cfg.get("max_reentries", 2)), store=self._lifecycle.store,
            ):
                self._tech_data_full[stock_code] = tech_data
                self._rejection_ledger.record(stock_code, {
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "entry_type": "再入场上限",
                    "benchmark_price": 0,
                    "stop_loss": 0,
                    "target_range": [],
                    "reasons": [f"再入场次数用尽（上限{int(reentry_cfg.get('max_reentries', 2))}次）："
                                "新事件不再重播，本标的转入长期观察"],
                    "hypothesis": {},
                    "fundamental": None,
                    "fundamental_rejected": False,
                    "valuation": None,
                    "valuation_rejected": False,
                })
                logger.info("%s 再入场次数用尽，本标的转入长期观察", stock_code)
                return []
        except Exception as e:
            logger.debug("再入场检查失败 %s: %s", stock_code, str(e)[:60])

        # 独立检查五种进场策略（【P3-1】趋势延续为第五策略）
        raw_signals = []
        for check_fn, _etype in [
            (self._check_panic_bottom, "恐慌抄底"),
            (self._check_arbitrage_entry, "套利低吸"),
            (self._check_momentum_chase, "确认追强"),
            (self._check_volume_breakout, "价量突破"),
            (self._check_trend_continuation, "趋势延续"),
        ]:
            sig = check_fn(stock_code, stock_name, tech_data, stop_loss_calc, market_mode, sector_status)
            if sig:
                raw_signals.append(sig)

        if not raw_signals:
            return []

        # 合并为一条综合信号
        merged = self._merge_entry_signals(raw_signals, tech_data, market_mode, sector_status)

        # 【一】可证伪假说构建 + 出厂检查（配对 Z/W 锚定，X/Y/Z/W 缺一即拒绝）
        from .hypothesis import build_entry_hypothesis, calc_atr_from_kline
        atr = calc_atr_from_kline(tech_data.get("kline") or [], period=14)

        valid_signals: List[EntrySignal] = []
        for sig in merged:
            sig.tech_data = tech_data
            from .signal_plan import build_execution_plan
            # 【层间接口修复】主档基准价 Y 随策略定位：
            # 追强类贴近触发位（突破当日跟进），低吸类保留 MA10 回踩档。
            # 旧实现一律 MA10 → 蘅东光确认追强 Y 距现价 10.3%、罗博特科
            # 趋势延续 13.5%，策略讲追强、档位给低吸（RRR 0.96 的病根）。
            main_tier_price, y_formula, y_inputs = entry_buypoint(
                sig.entry_type, sig.entry_trigger_price, tech_data, self._tc,
            )
            sig.entry_y_formula = y_formula
            sig.entry_y_inputs = y_inputs
            # 【四】配对止损：Z = X 的直接否定（结构位 - 波动率缓冲），
            # 替换原"现价锚定的支撑位止损"（沃尔德 9/4 倒挂根源：止损锚现价、
            # 买点锚 MA10，两个锚点错位 → 93.94 > 93.88）
            hyp = build_entry_hypothesis(
                entry_type=sig.entry_type,
                tech_data=tech_data,
                benchmark_price=main_tier_price,
                target_range=sig.target_range,
                trigger_reason=sig.trigger_reason,
                atr=atr,
                config=self._tc,
                stop_loss_fallback=sig.stop_loss,
            )
            sig.stop_loss = hyp.exit_z
            plan = build_execution_plan(
                entry_type=sig.entry_type,
                benchmark_price=main_tier_price,
                stop_loss=hyp.exit_z,
                target_range=sig.target_range,
                tech_data=tech_data,
                sector_status=sector_status,
                sector_name=getattr(sig, "sector_name", "") or getattr(sig, "sw_level2", ""),
                hypothesis_x=hyp.reason_x,
                hypothesis=hyp.as_dict(),
                atr=atr,
                data_guard=data_guard,
                gate_config=self._tc,
            )
            sig.execution_plan = plan.as_dict()
            # 【二】基本面摘要透传（推送展示 + 假说留痕）
            if plan.fundamental:
                verdict = (plan.fundamental.get("verdict") or {})
                note = str(verdict.get("note") or "")
                reasons = "/".join(str(r) for r in (verdict.get("reasons") or []))
                sig.fundamental_note = " | ".join(x for x in (note, reasons) if x)
                sig.hypothesis["fundamental"] = {
                    "note": note,
                    "verdict": verdict.get("verdict", ""),
                    "tags": verdict.get("tags", []),
                    "report_period": plan.fundamental.get("report_period", ""),
                }
            # 【Phase3】估值透镜摘要透传 + 假说留痕（估值维度进入记录闭环）
            if plan.valuation and isinstance(plan.valuation, dict):
                lens_verdict = plan.valuation.get("verdict") or {}
                lens_note = str(lens_verdict.get("note") or "")
                lens_reasons = "/".join(str(r) for r in (lens_verdict.get("reasons") or []))
                sig.valuation_note = " | ".join(x for x in (lens_note, lens_reasons) if x)
                sig.hypothesis["valuation"] = {
                    "note": lens_note,
                    "verdict": lens_verdict.get("verdict", ""),
                    "tags": lens_verdict.get("tags", []),
                    "pe_ttm": (plan.valuation.get("valuation") or {}).get("pe_ttm"),
                    "profit_abs": lens_verdict.get("profit_abs"),
                }
            if plan.hypothesis_rejected or plan.fundamental_rejected or plan.valuation_rejected:
                # 【一】/【二】/【Phase3】出厂拒绝：假说不完整、业绩雷或
                # 估值泡沫/模式性亏损追高 → 不进调度不推送，
                # 留痕供审计（signal_rejections 表）
                self._rejection_ledger.record(stock_code, {
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "entry_type": sig.entry_type,
                    "benchmark_price": plan.benchmark_price,
                    "stop_loss": plan.stop_loss,
                    "target_range": plan.target_range,
                    "reasons": list(plan.rejection_reasons),
                    "hypothesis": plan.hypothesis,
                    "fundamental": plan.fundamental,
                    "fundamental_rejected": plan.fundamental_rejected,
                    "valuation": plan.valuation,
                    "valuation_rejected": plan.valuation_rejected,
                })
                logger.warning(
                    "信号出厂被拒 %s %s: %s",
                    stock_code, sig.entry_type, "; ".join(plan.rejection_reasons),
                )
                continue
            sig.confidence = plan.confidence
            sig.benchmark_price = plan.benchmark_price
            sig.rrr_low = plan.rrr_low
            sig.rrr_high = plan.rrr_high
            sig.hypothesis = plan.hypothesis
            # 【推导栏置信度】执行计划建成后刷新 ④ 行：
            # 推导在 plan 之前生成，旧口径回退 EntrySignal 硬编码的高/中，
            # 与置信度栏 0/6 低矛盾（进第五轮）——现在两栏同源。
            sig.trigger_reason = self._refresh_derivation_confidence(
                sig.trigger_reason, plan,
            )
            valid_signals.append(sig)

        if not valid_signals:
            return []

        # 【三】事件诞生：通过出厂检查的信号注册生命周期
        # （N 日内回踩买点有效；收盘跌回突破位/板块退潮 → 立即撤单）
        # 【Phase5 回炉】事件携带规则版本（z_line_mode）：存量事件与新规则
        # 双轨可审计——观察卡显示"Z 按 XX 规则生成"，审计者不再误判
        # "Z 线统一没做"（博杰 9/7 旧 Z=85.54 实证）。
        current_z_mode = str(self._cfg("hypothesis_gate", "z_line_mode", default="buffered_structure"))
        tech_signals = tech_data.get("tech_signals") or {}
        ma5, ma10, ma20 = tech_data.get("ma5"), tech_data.get("ma10"), tech_data.get("ma20")
        ma_alignment = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
        projected_ratio = getattr(volume_snapshot, "projected_volume_vs_ma60", None)
        projection_mode = getattr(volume_snapshot, "projection_mode", "")
        projected_threshold = float(
            self._cfg("volume_projection", "breakout_threshold", default=1.2)
        )
        actual_volume = getattr(volume_snapshot, "volume_vs_ma60", None)
        projection_triggered = bool(
            projection_mode == "ok"
            and projected_ratio is not None
            and float(projected_ratio) >= projected_threshold
            and (actual_volume is None or float(actual_volume) <= 1.0)
        )
        entry_snapshot = {
            "volume_ratio": getattr(volume_snapshot, "volume_ratio", None),
            "volume_vs_ma60": actual_volume,
            "projected_volume_vs_ma60": projected_ratio,
            "projection_mode": projection_mode,
            "projection_threshold": projected_threshold,
            "projection_triggered": projection_triggered,
            "ma_alignment": ma_alignment,
            "rsi": tech_signals.get("rsi"),
            "sector_status": sector_status,
        }
        # P2-16：事件出生时落分型/位置，后验记分按策略/分型/位置三维累计。
        # 缺字段时 build_volume_pattern 逐级降级，不得因记分维度缺失拦截注册。
        try:
            from .volume_pattern import build_volume_pattern
            _vp = build_volume_pattern(tech_data)
            entry_snapshot["pattern"] = _vp.get("pattern") or ""
            entry_snapshot["position"] = _vp.get("position") or ""
        except Exception:
            pass
        for sig in valid_signals:
            try:
                event = self._lifecycle.register_event(
                    stock_code=sig.stock_code,
                    stock_name=sig.stock_name,
                    entry_type=sig.entry_type,
                    breakout_level=sig.hypothesis.get("z_reference") or sig.hypothesis.get("z", 0),
                    entry_price=sig.hypothesis.get("y") or main_tier_price,
                    stop_loss=sig.hypothesis.get("z") or sig.stop_loss,
                    target_low=(sig.hypothesis.get("w") or [0, 0])[0],
                    target_high=(sig.hypothesis.get("w") or [0, 0])[-1],
                    hypothesis=sig.hypothesis,
                    rule_version=current_z_mode,
                    y_formula=sig.entry_y_formula,
                    y_inputs=sig.entry_y_inputs,
                    entry_snapshot=entry_snapshot,
                )
                sig.event_id = event.event_id
            except Exception as e:
                logger.debug("事件注册失败 %s: %s", sig.stock_code, e)
        return valid_signals

    def _projection_cfg(self) -> Optional[Dict]:
        """【P0-1】外推配置：回测/日频数据禁用（K 线口径即全天量，外推是口径错配）。"""
        cfg = dict(self._cfg("volume_projection") or {})
        if self._backtest_mode:
            cfg["enabled"] = False
        return cfg

    def _check_panic_bottom(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        恐慌抄底 - 大盘恐慌 + 个股超卖

        D1 整改（2026-07-22）：固定跌幅 → 关键整数关口破位+尾盘确认
        ----------------------------------------------------------------
        制度前提：恐慌抄底依赖"恐慌必有托底"的政策预期（A股特色）
        在无政策托底环境（如2021-2023纯熊市）降级为"考虑"级信号

        触发条件（必要+充分）：
        必要条件（满足任一）：
          - 上证跌幅 > 4%（保留，作必要不充分）
          - 双创跌幅 > 5%
          - 涨跌比 < 0.15
        充分条件（必要条件满足后，再满足任一）：
          - 关键整数关口破位：上证收盘跌破 3000/3100/3200/.../4000 等100整百点位
          - 放量宣泄：成交量250日分位 > 70%
          - 极端放量：均量比 > 2.0 且上证跌 > 2%
        """
        panic_market = []
        panic_sufficient = []  # D1: 充分条件

        # === 必要条件：固定跌幅（保留但降级为必要不充分）===
        index_drop = tech_data.get("index_daily_drop", 0)
        idx_thresh = self._cfg("panic_bottom", "index_drop_threshold", default=4.0)
        if abs(index_drop) > idx_thresh:
            panic_market.append(f"上证暴跌{abs(index_drop):.1f}%")

        gem_star_drop = tech_data.get("gem_sci_tech_drop", 0)
        gem_thresh = self._cfg("panic_bottom", "gem_star_drop_threshold", default=5.0)
        if abs(gem_star_drop) > gem_thresh:
            panic_market.append(f"双创暴跌{abs(gem_star_drop):.1f}%")

        ad_ratio = tech_data.get("advance_decline_ratio", 1.0)
        ad_thresh = self._cfg("panic_bottom", "ad_ratio_extreme_weak", default=0.15)
        if ad_ratio < ad_thresh:
            panic_market.append(f"涨跌比{ad_ratio:.2f}(市场极弱)")

        # === D1 充分条件1: 关键整数关口破位 ===
        # 上证收盘跌破 100整百点位（3000/3100/3200/.../4000）
        # 从 tech_data 获取上证收盘价
        index_close = 0
        # 上证指数收盘价在 index_daily_drop 的计算源，这里用近似：
        # 如果有 index_kline 在 tech_data 里，取最后一日收盘
        # 否则用 current_price / (1 + index_drop/100) 反推
        try:
            mkt_env = get_market_environment()
            index_close = mkt_env.get('csi300_close', 0) or 0
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        # 兜底：用个股现价反推上证（粗略）
        if index_close <= 0:
            # 上证收盘价无法直接获取，用 index_drop 反推前收
            # 但这不够准确，跳过关键点位判定
            pass
        else:
            # 检查是否跌破关键整数关口（3000~4500，每100一个）
            for level in range(3000, 4600, 100):
                # 前一日在关口上方，今日收盘在关口下方
                prev_close_approx = index_close / (1 + index_drop / 100) if index_drop != 0 else index_close
                if prev_close_approx > level >= index_close:
                    panic_sufficient.append(f"上证跌破{level}关口(收{index_close:.0f})")
                    break

        # === D1 充分条件2: 放量宣泄（250日分位>70%）===
        try:
            mkt_env = get_market_environment()
            vol_pct = mkt_env.get("volume_percentile", 50)
            if vol_pct >= 70:
                panic_sufficient.append(f"放量宣泄(分位{vol_pct:.0f}%)")
            elif vol_pct < 30:
                # 缩量阴跌不宜抄底 — 从 panic_market 中移除已有的放量信号
                panic_market = [p for p in panic_market if "放量" not in p and "巨量" not in p]
                panic_market.append(f"缩量阴跌(分位{vol_pct:.0f}%)不宜抄底")
        except Exception as e:
            logger.debug("非关键异常: %s", e)

# D1: 极端放量已在上方 panic_market 中处理（删除重复块）

        # 放量判断（原有：20日均量比 + 新增：250日分位）
        market_vol = tech_data.get("market_volume_yi", 0)
        avg_vol_20 = tech_data.get("market_volume_20d_avg", 0)
        panic_vol_ratio = self._cfg("panic_bottom", "panic_volume_ratio", default=2.0)
        panic_vol_idx_drop = self._cfg("panic_bottom", "panic_volume_index_drop", default=2.0)
        sig_vol_ratio = self._cfg("panic_bottom", "significant_volume_ratio", default=1.5)
        if market_vol > 0 and avg_vol_20 > 0:
            vol_ratio = market_vol / avg_vol_20
            if vol_ratio > panic_vol_ratio and abs(index_drop) > panic_vol_idx_drop:
                panic_market.append(f"恐慌放量(均量{vol_ratio:.1f}倍)")
            elif vol_ratio > panic_vol_ratio:
                panic_market.append(f"巨量换手(均量{vol_ratio:.1f}倍)")
            elif vol_ratio > sig_vol_ratio:
                panic_market.append(f"显著放量(均量{vol_ratio:.1f}倍)")

        if not panic_market:
            return None

        # 过滤：缩量阴跌不宜抄底
        if any("不宜抄底" in p for p in panic_market):
            return None

        # D1: 必要条件（panic_market）+ 充分条件（panic_sufficient）双重确认
        # 原逻辑：必要条件≥1 即可触发（过松，2024段产生大量低质量信号）
        # 新逻辑：必要条件≥1 + 充分条件≥1 才触发
        # 如果没有充分条件，降级为"考虑"级信号（不触发买入，仅记录）
        if not panic_sufficient:
            # 无充分条件 — 降级，不触发
            return None

        stock_oversold = []
        current_price = tech_data.get("current_price", 0)
        change_pct = tech_data.get("change_pct", 0)

        # 1. 单日暴跌（短期恐慌抛售）
        stock_drop_thresh = self._cfg("panic_bottom", "stock_drop_threshold", default=-7)
        if change_pct and change_pct < stock_drop_thresh:
            stock_oversold.append(f"单日暴跌{abs(change_pct):.1f}%")

        # 2. RSI 超卖（反弹动能积累）
        rsi_val = tech_data.get("tech_signals", {}).get("rsi")
        if rsi_val is not None and rsi_val < 30:
            stock_oversold.append(f"RSI超卖({rsi_val:.0f})")

        # 3. 5日跌幅过大（短期暴跌）
        drop_5d = tech_data.get("drop_5d", 0)
        drop_5d_thresh = self._cfg("panic_bottom", "drop_5d_threshold", default=-0.15)
        if drop_5d < drop_5d_thresh:
            stock_oversold.append(f"5日跌{abs(drop_5d)*100:.1f}%")

        # 4. 跌破布林下轨（价格极端偏离）
        boll = tech_data.get("tech_signals", {}).get("bollinger", {})
        if boll and boll.get("position") == "below":
            stock_oversold.append("跌破布林下轨")

        # 5. 锤子线（抄底资金入场信号）
        if tech_data.get("has_hammer"):
            stock_oversold.append("锤子线")

        # 6. KDJ J 值极度超卖
        kdj = tech_data.get("tech_signals", {}).get("kdj", {})
        if kdj:
            j_val = kdj.get("j", 50)
            if j_val < 0:
                stock_oversold.append(f"KDJ超卖(J={j_val:.0f})")

        # 删除"跌破MA120" — 它是趋势指标不是超卖指标
        # 跌破MA120只说明价格离均线远，可能是长期下跌的正常状态，不代表超卖反弹

        if not stock_oversold:
            return None

        # D1: 市场恐慌(必要)+充分条件+个股超卖 三重确认
        all_conds = panic_market + panic_sufficient + stock_oversold
        trigger_reason = ";".join(all_conds)
        hc_market = self._cfg("panic_bottom", "high_confidence_market_conds", default=1)
        hc_stock = self._cfg("panic_bottom", "high_confidence_stock_conds", default=2)
        # D1: 高置信需要必要+充分+超卖都≥阈值
        confidence = "高" if (len(panic_market) >= hc_market and
                              len(panic_sufficient) >= 1 and
                              len(stock_oversold) >= hc_stock) else "中"

        return EntrySignal(
            stock_code=code, stock_name=name, entry_type="恐慌抄底",
            strategy_summary="恐慌抄底 — 大盘恐慌(暴跌/放量/涨跌比极低) + 个股超卖(暴跌/破MA120/锤子线)",
            entry_trigger_price=current_price, stop_loss=stop_loss.stop_loss_price,
            target_type="中线持有",
            target_range=self._calculate_target_range(tech_data, "恐慌抄底"),
            position_level="heavy",
            applicable_modes=["attack", "defend", "retreat"],
            sector_status=sector, trigger_reason=trigger_reason, confidence=confidence,
        )


    def _check_arbitrage_entry(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        套利低吸（第二种进场）
        条件：周线MACD向上 + 任一积极信号触发
        """
        if mode not in ("attack", "defend"):
            return None
        # 周线MACD过滤
        if not tech_data.get("weekly_macd_up"):
            return None

        conditions = []
        if tech_data.get("shrinking_pullback_ma5"):
            conditions.append("缩量回踩MA5")
        if tech_data.get("shrinking_pullback_ma10"):
            conditions.append("缩量回踩MA10")
        if tech_data.get("pair_bottom"):
            conditions.append("对子底出现")
        if tech_data.get("daily_limit_opened"):
            conditions.append("跌停板被撬开")

        min_conds = self._cfg("arbitrage", "min_trigger_conditions", default=1)
        if len(conditions) >= min_conds:
            return EntrySignal(
                stock_code=code,
                stock_name=name,
                entry_type="套利低吸",
                entry_trigger_price=tech_data.get("current_price", 0),
                stop_loss=stop_loss.stop_loss_price,
                target_type="冲高止盈",
                target_range=self._calculate_target_range(tech_data, "套利低吸"),
                position_level="arbitrage",
                applicable_modes=["attack", "defend"],
                sector_status=sector,
                trigger_reason="；".join(conditions),
                confidence="中",
            )
        return None

    def _defensive_chase_gates(self, tech_data: Dict, sector_status: str) -> Dict:
        """【P1-1】防守模式确认追强的三重门（降仓可用，不是无条件放行）。

        门一 四确认+时机：创新高 + 量比 + 量能（外推口径）+ ADX 单边力度
                          + 外盘主动 + RSI 未过热；
        门二 基本面验证：净利同比达门槛且非业绩雷/低基数/模式亏损，
                        筹码无分散（减持迹象）；
        门三 板块联动：主线板块（主线意味着联动度，非孤立动量）。
        任何一关不过 → 不放行（回退为禁用，磨空成本由 unified_engine 留档）。
        """
        cfg = self._cfg("defensive_chase") or {}
        evidence: List[str] = []

        # ── 门一：四确认 + 时机 ──
        # 【P1-2】每个闸门都输出 当前值/阈值/差距，读者可复现候梯排序。
        def _gap(label: str, value, threshold: float, direction: str = "ge",
                 fmt: str = "{:.2f}") -> Tuple[str, float]:
            """direction=ge 表示越大越好；lt 表示越小越好。返回(展示, 缺口%)。"""
            if value is None:
                return f"{label} 数据未取到(阈值{fmt.format(threshold)})", 100.0
            value = float(value)
            if direction == "ge":
                ok = value >= threshold
                gap_pct = max(0.0, (threshold - value) / abs(threshold) * 100.0) if threshold else 0.0
                rel = "缺" if not ok else "✓"
            else:
                ok = value <= threshold
                gap_pct = max(0.0, (value - threshold) / abs(threshold) * 100.0) if threshold else 0.0
                rel = "超" if not ok else "✓"
            return (
                f"{label} {fmt.format(value)}/{fmt.format(threshold)} "
                + (f"{rel}{gap_pct:.0f}%" if not ok else "✓"),
                gap_pct,
            )

        current = float(tech_data.get("current_price") or 0)
        recent_high = float(tech_data.get("recent_high") or 0)
        volume_ratio = float(tech_data.get("volume_ratio") or 1.0)
        adx = tech_data.get("adx") or (tech_data.get("tech_signals") or {}).get("adx")
        rsi = tech_data.get("rsi") or (tech_data.get("tech_signals") or {}).get("rsi")
        outer = tech_data.get("outer_volume")
        inner = tech_data.get("inner_volume")

        from .signal_plan import build_volume_snapshot
        snapshot = build_volume_snapshot(
            tech_data, guard=self._cfg("data_guard") or None,
            projection_config=self._projection_cfg(),
        )
        projected = snapshot.projected_volume_vs_ma60

        new_high_ratio = float(cfg.get("new_high_ratio", 0.99))
        volume_ratio_min = float(cfg.get("volume_ratio_min", 1.2))
        projected_volume_min = float(cfg.get("projected_volume_min", 1.2))
        adx_min = float(cfg.get("adx_min", 25.0))
        rsi_overheat = float(cfg.get("rsi_overheat", 67.0))

        # 【Phase5 回炉】“创新高”用 prior_high（近20日高剔除当日）——
        # 盘中创新高但收盘回落 >1% 的标的（蘅东光 9/7）不再被误拦；
        # prior_high 缺失时回退 recent_high（兼容旧测试/旧缓存数据）。
        prior_high = tech_data.get("prior_high") or recent_high
        gap_items: List[Dict] = []
        gate1_checks = {}
        gate1_text = []
        if current and prior_high:
            text, gap = _gap(
                "创新高", current, float(prior_high) * new_high_ratio,
                "ge", "{:.2f}",
            )
            gate1_checks["创新高"] = gap == 0.0
            gate1_text.append(text)
            gap_items.append({"gate": "确认追强", "item": "创新高", "gap": gap, "direction": "ge"})
        else:
            gate1_checks["创新高"] = False
            gate1_text.append(f"创新高 数据未取到(阈值{new_high_ratio:.2f}×近段高)")
            gap_items.append({"gate": "确认追强", "item": "创新高", "gap": 100.0, "direction": "ge"})

        text, gap = _gap("量比", volume_ratio, volume_ratio_min, "ge", "{:.2f}")
        gate1_checks["量比"] = gap == 0.0
        gate1_text.append(text)
        gap_items.append({"gate": "确认追强", "item": "量比", "gap": gap, "direction": "ge"})

        projected_ok = projected is not None and snapshot.projection_mode == "ok"
        volume_value = projected if projected_ok else snapshot.volume_vs_ma60
        volume_threshold = projected_volume_min if projected_ok else 1.0
        volume_label = "量能外推" if projected_ok else "量能实际"
        text, gap = _gap(volume_label, volume_value, volume_threshold, "ge", "{:.2f}x")
        gate1_checks["量能(外推或实际)"] = gap == 0.0
        gate1_text.append(text)
        gap_items.append({"gate": "确认追强", "item": "量能", "gap": gap, "direction": "ge"})

        text, gap = _gap("ADX", adx, adx_min, "ge", "{:.1f}")
        gate1_checks["ADX单边力度"] = gap == 0.0
        gate1_text.append(text)
        gap_items.append({"gate": "确认追强", "item": "ADX", "gap": gap, "direction": "ge"})

        if outer is None or inner is None:
            gate1_checks["外盘主动"] = True
            gate1_text.append("外盘主动 数据未取到(不阻断)")
        else:
            text, gap = _gap("外盘/内盘", float(outer), float(inner), "ge", "{:.0f}")
            gate1_checks["外盘主动"] = gap == 0.0
            gate1_text.append(text)
            gap_items.append({"gate": "确认追强", "item": "外盘主动", "gap": gap, "direction": "ge"})

        # 【Phase5 回炉】文案带口径：实际判定用 RSI14；同条件两判定的口径差异可见。
        text, gap = _gap("RSI14", rsi, rsi_overheat, "lt", "{:.1f}")
        gate1_checks[f"RSI14未过热(<{rsi_overheat:.0f})"] = gap == 0.0
        gate1_text.append(f"RSI14未过热(<{rsi_overheat:.0f})：{text}")
        gap_items.append({"gate": "确认追强", "item": "RSI14", "gap": gap, "direction": "lt"})

        gate1_failed = [k for k, v in gate1_checks.items() if not v]
        if gate1_failed:
            evidence.append(f"门一未过: {'、'.join(gate1_text)}")

        # ── 门二：基本面验证 ──
        fundamental = tech_data.get("fundamental") or {}
        profit_yoy = None
        profit_abs = None
        try:
            profit_yoy = float(fundamental.get("profit_yoy")) if fundamental.get("profit_yoy") is not None else None
            profit_abs = float(fundamental.get("profit_abs")) if fundamental.get("profit_abs") is not None else None
        except (TypeError, ValueError):
            pass
        verdict = (fundamental.get("verdict") or {}).get("verdict") if isinstance(fundamental.get("verdict"), dict) else None
        lens = tech_data.get("valuation_lens") or {}
        lens_tags = ((lens.get("verdict") or {}).get("tags") or []) if isinstance(lens, dict) else []
        shareholder_change = None
        try:
            votes = (tech_data.get("institutional_holding") or {}).get("votes") or {}
            raw = (votes.get("shareholder") or {}).get("raw") or {}
            if raw.get("change_pct") is not None:
                shareholder_change = float(raw.get("change_pct"))
        except (TypeError, ValueError, AttributeError):
            pass

        profit_yoy_min = float(cfg.get("profit_yoy_min", 30.0))
        dispersion_max = float(cfg.get("shareholder_dispersion_max", 0.20))
        profit_text, profit_gap = _gap(
            "净利同比", profit_yoy, profit_yoy_min, "ge", "{:.1f}%"
        )
        if profit_gap:
            gap_items.append({"gate": "确认追强", "item": "净利同比", "gap": profit_gap, "direction": "ge"})
        dispersion_text, dispersion_gap = _gap(
            "筹码分散", shareholder_change, dispersion_max, "lt", "{:.1%}"
        )
        if shareholder_change is not None and dispersion_gap:
            gap_items.append({"gate": "确认追强", "item": "筹码分散", "gap": dispersion_gap, "direction": "lt"})
        gate2_checks = {
            "净利同比达标": bool(profit_yoy is not None and profit_yoy >= profit_yoy_min),
            "无业绩雷/降级": bool(verdict not in ("veto", "warn")) if verdict else (profit_yoy is not None),
            "非低基数/模式亏损": not any(t in lens_tags for t in ("low_base", "model_loss")),
            "筹码无分散": bool(
                shareholder_change is None or shareholder_change <= dispersion_max
            ),
        }
        gate2_failed = [k for k, v in gate2_checks.items() if not v]
        if gate2_failed:
            evidence.append(f"门二未过: 净利同比达标；{profit_text}；筹码无分散：{dispersion_text}")

        # ── 门三：板块联动 ──
        if sector_status != "main_trend":
            sector_names = {
                "rotational": "轮动", "retreating": "退潮",
                "main_trend": "主线", "unknown": "未知",
            }
            evidence.append(
                f"门三未过: 板块非主线({sector_names.get(sector_status, sector_status)}"
                f"/主线，差距不可量化)"
            )

        tech_data["defensive_gate_audit"] = {
            "gap_items": gap_items,
            "check_total": len(gate1_checks) + len(gate2_checks) + 1,
            "checks_passed": (
                sum(1 for value in gate1_checks.values() if value)
                + sum(1 for value in gate2_checks.values() if value)
                + (1 if sector_status == "main_trend" else 0)
            ),
        }
        return {
            "passed": not evidence,
            "evidence": evidence,
            "gap_items": gap_items,
            "check_total": len(gate1_checks) + len(gate2_checks) + 1,
            "checks_passed": (
                sum(1 for value in gate1_checks.values() if value)
                + sum(1 for value in gate2_checks.values() if value)
                + (1 if sector_status == "main_trend" else 0)
            ),
            "min_gate_gap": min((x["gap"] for x in gap_items), default=100.0),
            "projected_volume": projected,
            "snapshot": snapshot,
        }

    def _check_momentum_chase(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        确认追强（海龟突破）

        【P1-1】防守模式降仓可用：防守的定义是压缩敞口而非禁用策略
        （蘅东光 9/7 +14.81%、量比2.01、ADX48 却零提示），
        但必须过三重门（四确认+基本面+板块联动），仓位由风险预算反推。
        """
        if mode not in ("attack", "defend"):
            return None
        defensive_gate = None
        if mode == "defend":
            cfg = self._cfg("defensive_chase") or {}
            if not cfg.get("enabled", True):
                return None  # 回退禁用（磨空成本由 unified_engine 留档）
            defensive_gate = self._defensive_chase_gates(tech_data, sector)
            if not defensive_gate["passed"]:
                return None

        current = tech_data.get("current_price", 0)
        ma20 = tech_data.get("ma20", 0)
        recent_high = tech_data.get("recent_high", 0)
        # 【Phase5 回炉】突破判定锚 prior_high（前期高点，剔除当日）：
        # 旧口径 current≥recent_high(含当日高点)×0.99 实际测度的是
        # “收盘接近日内最高”——盘中冲高回落 2% 的真突破被误拦
        # （蘅东光 9/7 类形态）。海龟突破的语义本就是“突破前期高点”。
        prior_high = tech_data.get("prior_high") or recent_high
        vol_ratio = tech_data.get("volume_ratio", 1.0)
        kline = tech_data.get("kline", [])

        if not all([current, ma20, prior_high]):
            return None

        # MA20趋势向上
        ma20_min_klines = self._cfg("momentum_chase", "ma20_trend_min_klines", default=21)
        ma20_window = self._cfg("momentum_chase", "ma20_window", default=20)
        if len(kline) >= ma20_min_klines:
            try:
                closes = [float(k.get("收盘", k.get("close", 0))) for k in kline]
                yesterday_ma20 = sum(closes[-ma20_min_klines:-1]) / ma20_window
                if ma20 <= yesterday_ma20:
                    return None
            except Exception:
                return None
        else:
            return None

        # Donchian 20日突破（锚前期高点）
        breakout_ratio = self._cfg("momentum_chase", "breakout_price_ratio", default=0.99)
        if current < float(prior_high) * breakout_ratio:
            return None

        # 放量确认
        vol_confirm = self._cfg("momentum_chase", "volume_confirm_ratio", default=1.2)
        if vol_ratio < vol_confirm:
            return None

        conditions = [
            f"海龟突破(MA20趋势向上, 突破前高{float(prior_high):.2f}, 放量{vol_ratio:.1f}倍)",
        ]
        position_level = "spread"
        applicable_modes = ["attack"]
        if defensive_gate is not None:
            # 防守模式降仓放行：三重门全过 + 风险预算仓位（试探仓）
            position_level = "probe"
            applicable_modes = ["defend"]
            conditions.append(
                "防守降仓放行(三重门全过: 四确认+基本面+板块联动；"
                "仓位=单笔风险预算÷止损距离，非 1/3 惯例)"
            )
        # 【P0-2】高位回落票不允许把 MA20 突破机械当作低位突破；
        # 必须量能（已过）+资金同向双重确认，否则只观察不发买入。
        deep_pullback, drawdown_pct, high_52w = self._deep_pullback_from_52w(tech_data)
        if deep_pullback and not self._fund_flow_confirms(tech_data):
            tech_data["entry_blocked_reason"] = (
                f"高位回落{drawdown_pct:.1f}%，突破需资金同向；"
                f"资金投票未确认，仅观察"
            )
            return None
        return EntrySignal(
            stock_code=code,
            stock_name=name,
            entry_type="确认追强",
            strategy_summary="确认追强 — 突破关键位 + 放量确认 + RS线强势",
            entry_trigger_price=current,
            stop_loss=stop_loss.stop_loss_price,
            target_type="突破持有",
            target_range=self._calculate_target_range(tech_data, "确认追强"),
            position_level=position_level,
            applicable_modes=applicable_modes,
            sector_status=sector,
            trigger_reason="；".join(conditions),
            confidence="高",
            defensive_chase=(defensive_gate or None) and {
                "evidence": defensive_gate["evidence"],
                "projected_volume": defensive_gate["projected_volume"],
            },
        )

    def _check_trend_continuation(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        【P3-1】趋势延续：突破后 1~10 日的延续段回踩入场

        沃尔德 9/7 +7.02% 处于突破后延续段（MA多头排列、MACD金叉延续、
        回踩不破 MA10 再放量），四种策略无一覆盖：
        - 价量突破要求突破当日诞生（事件边界），延续段不再触发；
        - 确认追强防守禁用（P1-1 仅覆盖创新高当日）；
        - 套利低吸要求周线 MACD + 缩量回踩形态，延续段是放量非缩量。

        延续段逻辑：趋势已确立，入场点是回踩均线不破 + 再度放量；
        Z = MA10（延续结构破位即逻辑死亡），W = 延伸目标位。
        """
        if mode not in ("attack", "defend"):
            return None
        cfg = self._cfg("trend_continuation") or {}
        if not cfg.get("enabled", True):
            return None

        current = float(tech_data.get("current_price") or 0)
        ma5 = float(tech_data.get("ma5") or 0)
        ma10 = float(tech_data.get("ma10") or 0)
        ma20 = float(tech_data.get("ma20") or 0)
        ma25 = float(tech_data.get("ma25") or 0)
        recent_high = float(tech_data.get("recent_high") or 0)
        vol_ratio = float(tech_data.get("volume_ratio") or 1.0)
        kline = tech_data.get("kline") or []
        if not all([current, ma5, ma10, ma20, ma25, recent_high]) or len(kline) < 30:
            return None

        # ① 趋势结构：MA 多头排列 + 现价在 MA25 上方（趋势内，非突破当日）
        require_alignment = bool(cfg.get("ma_alignment", True))
        if require_alignment and not (ma5 > ma10 > ma20):
            return None
        if current <= ma25:
            return None

        # ② 延续段边界：突破不是今日发生（昨收已在 MA25 上方 → 非当日事件），
        #    且距近 20 日新高在延续窗口内（突破后 1~10 日）
        prev_close = float(tech_data.get("prev_close") or 0)
        ma25_prev = tech_data.get("ma25_prev")
        if prev_close and ma25_prev and prev_close > float(ma25_prev):
            # 昨日已在 MA25 上方 → 非当日突破，是延续段候选
            pass
        else:
            return None  # 突破当日归价量突破，本策略不重复覆盖

        # ③ 距 20 日新高的距离在延续窗口内（突破后第 1~10 日）
        # 【Phase5 回炉】锚 prior_high（剔除当日）：当日冲高不再把“距新高天数”
        # 虚抬高，延续段计数回到真实突破日起算。
        max_bars = int(cfg.get("max_bars_since_breakout", 10))
        closes = [
            float(k.get("收盘", k.get("close", 0)) or 0) for k in kline[-(max_bars + 5):]
        ]
        prior_high_ref = float(tech_data.get("prior_high") or recent_high)
        if not closes or prior_high_ref <= 0:
            return None
        bars_since_high = 0
        for close in reversed(closes[:-1]):
            if close >= prior_high_ref * float(cfg.get("new_high_ratio", 0.99)):
                break
            bars_since_high += 1
        if bars_since_high < int(cfg.get("min_bars_since_breakout", 1)) - 1:
            return None
        if bars_since_high > max_bars:
            return None

        # ④ 回踩不破 + 再度放量：现价回踩 MA5/MA10 附近但未破 MA10，
        #    且当日再度放量（延续段的入场确认）
        vol_min = float(cfg.get("volume_ratio_min", 1.2))
        if vol_ratio < vol_min:
            return None
        if current < ma10:
            return None

        conditions = [
            f"突破后延续段(第{bars_since_high + 1}日, MA多头排列, 现价{current:.2f}站上MA10:{ma10:.2f})",
            f"回踩不破再度放量(量比{vol_ratio:.1f}倍)",
        ]
        return EntrySignal(
            stock_code=code,
            stock_name=name,
            entry_type="趋势延续",
            strategy_summary="趋势延续 — 突破后延续段回踩不破 + 再度放量（沃尔德 9/7 类形态）",
            entry_trigger_price=current,
            stop_loss=stop_loss.stop_loss_price,
            target_type="主升持有",
            target_range=self._calculate_target_range(tech_data, "确认追强"),
            position_level="normal",
            applicable_modes=["attack", "defend"],
            sector_status=sector,
            trigger_reason="；".join(conditions),
            confidence="中",
        )

    def _check_volume_breakout(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        # D7 价量突破分时段审计（2026-07-25 整改）
        # 原：整体胜率42.3%，需分时段审计（震荡市证伪）
        # 日频引擎无法做分时段，实盘由 orchestrator 在 9:30-9:31/14:55-14:57 时段过滤
        """
        价量突破型（右侧个股级突破，不依赖板块启动）
        """
        if mode not in ("attack", "defend"):
            return None
        # RS line 过滤
        require_rs = self._cfg("volume_breakout", "require_rs_above_ma", default=True)
        if mode == "attack" and require_rs:
            rs = tech_data.get("rs_line", {})
            # 修复 BUG-B3: RS 数据缺失时不阻断（只在有完整 RS 数据时才过滤）
            if rs and "rs_latest" in rs and "rs_ma" in rs:
                if rs.get("rs_latest", 0) <= rs.get("rs_ma", float('inf')):
                    return None

        current = tech_data.get("current_price", 0)
        ma25 = tech_data.get("ma25", 0)
        prev_close = tech_data.get("prev_close", 0)
        if not all([current, ma25, prev_close]):
            return None

        # 【三】事件边界（状态 → 事件）：
        # "站上 MA25"是状态——今天为真、明天也为真，导致同一信号连发两天。
        # 事件化：昨收在昨日 MA25 下方（或贴线），今日站上今日 MA25 → 突破当日诞生。
        # 注意 _fetch_tech_data 已算好 ma25_prev（昨日 MA25），此前被刻意弃用。
        require_event_boundary = self._cfg(
            "volume_breakout", "require_event_boundary", default=True
        )
        if require_event_boundary:
            ma25_prev = tech_data.get("ma25_prev")
            if ma25_prev and prev_close:
                crossed_today = prev_close <= ma25_prev * 1.002 and current > ma25
                if not crossed_today:
                    return None
            else:
                # 无昨日数据无法判定事件边界 → 拒绝（宁可漏过，不可重播）
                return None

        # 【三】诞生双腿之一：当日收阳（日内以 现价>今开 近似收盘态）
        require_bullish = self._cfg(
            "volume_breakout", "require_bullish_close", default=True
        )
        if require_bullish:
            today_open = tech_data.get("today_open") or 0
            if not today_open:
                kline = tech_data.get("kline") or []
                if kline:
                    today_open = float(kline[-1].get("开盘", kline[-1].get("open", 0)) or 0)
            if today_open and current <= today_open:
                return None

        # 事件有效期内价格仍需站上 MA25（跌回突破位的由生命周期失效逻辑撤单）
        price_above_ma25 = current > ma25
        if not price_above_ma25:
            return None

        # 修复 BUG-E2: 涨停日放宽量能要求
        # A 股涨停日缩量是正常现象（卖方惜售），涨停本身就算强势确认
        # 检测涨停：当日涨幅 >= 涨停比例的 99.5%
        try:
            from ..loop.backtest_engine import get_limit_ratio
        except ImportError:
            from src.loop.backtest_engine import get_limit_ratio
        is_limit_up_today = False
        if prev_close > 0:
            chg_pct = (current - prev_close) / prev_close
            limit_ratio = get_limit_ratio(code)
            if chg_pct >= limit_ratio * 0.995:
                is_limit_up_today = True

        # 量能突破 60日均量线（涨停日豁免）
        from .signal_plan import build_volume_snapshot

        # 触发、置信度和分档必须读同一份量能快照，避免原始字段和分位口径分裂。
        # 【P0-1】外推封板判定需按代码的涨跌停幅度（北交所 30%，
        # 蘅东光 +14.81% 不是封板；主板 10% 才是）。
        volume_snapshot = build_volume_snapshot(
            tech_data, guard=self._cfg("data_guard") or None,
            projection_config=self._projection_cfg(),
            limit_ratio=get_limit_ratio(code),
        )
        if volume_snapshot.volume_vs_ma60 is None:
            return None

        # 【P0-1】外推口径：盘中累计量/全天均量结构性偏小（分子只走了半天），
        # 午前 1.02x 在外推口径下 = 2.0x（蘅东光 9/7 实证）。
        # 实际口径 >1.0 或 外推口径 ≥阈值（比实际口径更严，对冲开盘冲量高估）
        # 任一成立即量能确认。
        projected = volume_snapshot.projected_volume_vs_ma60
        projected_threshold = float(
            self._cfg("volume_projection", "breakout_threshold", default=1.2)
        )
        volume_breakout = (
            volume_snapshot.volume_vs_ma60 is not None
            and volume_snapshot.volume_vs_ma60 > 1.0
        )
        projected_breakout = (
            projected is not None
            and projected >= projected_threshold
            and volume_snapshot.projection_mode == "ok"
        )
        if not volume_breakout and not projected_breakout and not is_limit_up_today:
            return None

        # 【P0-2】52周高点回落超过25%的“低位反抽”，单看量能不够：
        # 要求量能（已过）+资金同向双重确认，否则只观察不发买入。
        deep_pullback, drawdown_pct, high_52w = self._deep_pullback_from_52w(tech_data)
        if deep_pullback and not self._fund_flow_confirms(tech_data):
            tech_data["entry_blocked_reason"] = (
                f"距52周高回落{drawdown_pct:.1f}%，突破需资金同向；"
                f"资金投票未确认，仅观察"
            )
            return None

        vol_ratio = volume_snapshot.volume_vs_ma60 or 0
        if volume_breakout:
            volume_text = f"量能突破60日均量({vol_ratio:.1f}倍)"
        elif projected_breakout:
            volume_text = (
                f"量能突破60日均量(外推口径{projected:.1f}倍，"
                f"累计{vol_ratio:.2f}x，{volume_snapshot.projection_note})"
            )
        else:
            volume_text = f"涨停豁免量能(涨幅{(current-prev_close)/prev_close*100:.1f}%)"
        conditions = [
            f"突破MA25(昨收{prev_close:.2f}在昨日MA25下方，今价{current:.2f}站上{ma25:.2f})",
            volume_text,
        ]

        return EntrySignal(
            stock_code=code,
            stock_name=name,
            entry_type="价量突破",
            strategy_summary="价量突破 — 价格上穿MA25 + 量能突破60日均量 + RS线强势",
            entry_trigger_price=current,
            stop_loss=stop_loss.stop_loss_price,
            target_type="突破持有",
            target_range=self._calculate_target_range(tech_data, "价量突破"),
            position_level="normal",
            applicable_modes=["attack", "defend"],
            sector_status=sector,
            trigger_reason="；".join(conditions),
            confidence="高",
        )

    def _deep_pullback_from_52w(self, tech_data: Dict[str, Any]) -> tuple[bool, float, Optional[float]]:
        """判断价格距 52 周高点是否深度回落；数据不足时不制造低位结论。"""
        current = _number_or_none(tech_data.get("current_price"))
        if current is None or current <= 0:
            return False, 0.0, None

        high_52w = _number_or_none(
            tech_data.get("high_52w", tech_data.get("high_250d"))
        )
        if high_52w is None or high_52w <= 0:
            kline = tech_data.get("kline") or []
            if len(kline) < 250:
                return False, 0.0, None
            highs = [
                _number_or_none(k.get("最高", k.get("high")))
                for k in kline[-250:]
            ]
            highs = [value for value in highs if value is not None and value > 0]
            if not highs:
                return False, 0.0, None
            high_52w = max(highs)

        drawdown_pct = max(0.0, (high_52w / current - 1.0) * 100.0)
        return drawdown_pct >= 25.0, drawdown_pct, high_52w

    def _fund_flow_confirms(self, tech_data: Dict[str, Any]) -> bool:
        """52周深度回落票的资金确认：完整覆盖窗口且主力净流同向。"""
        from .signal_plan import build_fund_snapshot

        fund = build_fund_snapshot(tech_data.get("institutional_holding"), tech_data)
        return bool(fund.flow_coverage_complete and fund.main_vote > 0)

    # ============ 出场信号 ============

    def _volume_context(self, tech_data: Dict[str, Any]) -> str:
        """输出量能的判定口径，禁止只给一个无法复核的量比。"""
        snapshot = tech_data.get("tech_signals", {}).get("volume_snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        early = bool(snapshot.get("is_early_window"))
        ratio = (
            snapshot.get("same_period_volume_ratio")
            if early
            else snapshot.get("volume_ratio_effective", tech_data.get("volume_ratio"))
        )
        try:
            ratio = float(ratio) if ratio is not None else None
        except (TypeError, ValueError):
            ratio = None
        if ratio is None:
            if early:
                return "量能:数据未取到(早盘需同期量比)"
            return "量能:数据未取到"
        label = "同期量比" if early else "接口量比"
        sample_time = (
            snapshot.get("same_period_sample_time")
            if early
            else snapshot.get("volume_ratio_sample_time")
        )
        source = (
            snapshot.get("same_period_caliber")
            if early
            else snapshot.get("volume_ratio_caliber")
            or tech_data.get("volume_ratio_source")
            or "口径未标注"
        )
        time_text = f"@{sample_time}" if sample_time else "@时间未标注"
        text = f"量能:{label}{ratio:.2f}x@{sample_time or '时间未标注'}(口径:{source}"
        prev_ratio = snapshot.get("volume_vs_prev_day")
        try:
            prev_ratio = float(prev_ratio) if prev_ratio is not None else None
        except (TypeError, ValueError):
            prev_ratio = None
        if prev_ratio is not None:
            text += f",较前日{prev_ratio:.2f}x"
        # B-修复：早盘量能证据用于卖出时显式豁免说明（卖出风控从宽、买入确认从严），
        # 与买入分型“早盘只展示不计票”门槛的差异必须写明（盛合晶微/长光华芯9-18实证）。
        if early:
            text += "；早盘量能仅作卖出风控从宽计入(买入确认从严)"
        return text + ")"

    def check_exit_signals(
        self,
        stock_code: str,
        stock_name: str,
        market_mode: str = "defend",
        sector_status: str = "rotational",
        sector_name: str = "",
    ) -> List[ExitSignal]:
        """
        检查持仓股出场信号
        卖出信号始终推送，不受任何模式约束

        买入/卖出只是信号，不需要持仓成本价。
        MA5 压制止盈改为纯技术面判断：跌破 MA5 且 MA5 仍上升（趋势破坏）。

        P1-4 结构说明（443行，便于维护）：
        - 块1: 止损价计算 + 破位止损（C1硬触发）
        - 块2: 上涨衰竭信号收集（RSI/上影线/MA5乖离/KDJ）
        - 块3: MA5压制（C2分批trailing）
        - 块4: C3四条卖出规则（观察级）
        - 块5: 技术走弱（投票偏空/布林下轨）
        - 块6: 信号合并+推导链
        """
        signals = []

        tech_data = self._fetch_tech_data(stock_code, market_mode)
        # 缓存完整 tech_data（含技术指标+机构打分），供 engine.py 观察列表复用
        self._tech_data_full[stock_code] = tech_data
        stop_loss_calc = self.calculate_stop_loss(stock_code, tech_data)

        current_price = tech_data.get("current_price", 0)
        if not current_price or float(current_price) <= 0:
            # 行情缺失时 0/0 会伪装成破位；宁可漏评一次，也不能生成无效卖出。
            self._exit_diagnostics[stock_code] = "卖出检查: 现价缺失，数据不足，未评估"
            logger.warning("卖出检查跳过 %s: 现价缺失", stock_code)
            return []

        # ============================================================
        # 【四】Block 0: 策略配对出场——读取该持仓的入场假说（X/Y/Z/W）
        # 每个策略在定义买入的那一刻就定义卖出：Z 是 X 的直接否定（硬触发），
        # W 是兑现位（价位触发，与进场同颗粒度）；旧漂移止损+投票门降为辅助观察。
        # 持仓策略未知（无回执记录）时退回旧系统兑底逻辑。
        # ============================================================
        paired_position = self._get_paired_position(stock_code)
        paired_strategy = (paired_position or {}).get("entry_type") or ""
        if paired_position and (paired_position.get("paired_z") or 0) > 0:
            # C1 破位止损锚定到配对 Z（X 的直接否定），
            # 不再用"现价锚定支撑位"的漂移止损（沃尔德式两个锚点错位根源）
            stop_loss_calc.stop_loss_price = float(paired_position["paired_z"])
            paired_ref = float(paired_position.get("z_reference") or 0)
            if paired_ref > 0:
                stop_loss_calc.chosen_support = paired_ref

        # 技术指标投票 + K 线形态
        tech_vote = tech_data.get("tech_signals", {}).get("vote", "中性")
        tech_score = tech_data.get("tech_signals", {}).get("vote_score", 0)
        kline_patterns = tech_data.get("kline_pattern", [])

        _bearish_votes = {"强烈看空", "偏空", "温和偏空"}
        is_bearish = tech_vote in _bearish_votes

        # 【P0-1】结构位与止损之间的灰区不再是空白：收盘跌破结构位给出
        # 减仓/清仓指令；量比缺失时保守给减仓，不伪造“持有正常”。
        gray_zone = None
        if paired_position and (paired_position.get("z_reference") or 0) > 0:
            structure_ref = float(paired_position["z_reference"])
            paired_stop = float(stop_loss_calc.stop_loss_price)
            if structure_ref > paired_stop > 0 and paired_stop < current_price < structure_ref:
                try:
                    gray_volume = (
                        float(tech_data.get("volume_ratio"))
                        if tech_data.get("volume_ratio") is not None else None
                    )
                except (TypeError, ValueError):
                    gray_volume = None
                if gray_volume is not None and gray_volume < 0.8:
                    gray_action = "灰区清仓"
                    gray_urgency = "紧急"
                    volume_text = f"量比{gray_volume:.2f}<0.8，缩量失守"
                else:
                    gray_action = "灰区减仓"
                    gray_urgency = "重要"
                    volume_text = (
                        f"量比{gray_volume:.2f}≥0.8" if gray_volume is not None
                        else "量比数据未取到，按保守减仓"
                    )
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type=gray_action,
                    trigger_price=current_price,
                    stop_loss_price=paired_stop,
                    reason=(
                        f'收盘{current_price:.2f}跌破结构位{structure_ref:.2f}，'
                        f'未到止损{paired_stop:.2f}；{volume_text}，'
                        f'执行{gray_action[2:]}（持有/减仓/清仓中取一）'
                    ),
                    urgency=gray_urgency, mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name,
                    tech_data=tech_data, source="paired",
                    paired_strategy=paired_strategy,
                ))

        # 1. 破位止损 — C1 整改（2026-07-22）：硬触发
        # ----------------------------------------------------------------
        # 原逻辑：三重确认（收盘价+量比>0.8+技术偏空）才触发，过严导致514笔0触发
        # 新逻辑：跌破止损价即执行（破位止损，紧急），量能/投票只决定推送级别
        #   - 现价 ≤ 止损价 → 破位止损（紧急，硬触发）
        #   - 量能/投票信息附加到 reason，但不影响触发决策
        # 帖43"先止损再说"：出场永远比进场果断，不讨价还价
        stop_triggered = (
            current_price > 0
            and stop_loss_calc.stop_loss_price > 0
            and current_price <= stop_loss_calc.stop_loss_price
        )
        if stop_triggered:
            vol_ratio = tech_data.get('volume_ratio')
            if vol_ratio is not None:
                try:
                    vol_ratio = float(vol_ratio)
                except (TypeError, ValueError):
                    vol_ratio = None
            heavy_vol_thresh = self._cfg("exit", "breakdown", "heavy_volume_ratio", default=1.3)

            # C1: 硬触发，不再要求三重确认
            if paired_position:
                reason = (f'跌破配对止损Z={stop_loss_calc.stop_loss_price:.2f}'
                          f'（[{paired_strategy}]买入理由的直接否定，X 被证伪），硬触发')
            else:
                reason = f'跌破{stop_loss_calc.chosen_support:.2f}(止损{stop_loss_calc.stop_loss_price:.2f})，硬触发'
            if current_price > 0 and stop_loss_calc.stop_loss_price > 0:
                stop_gap_pct = (
                    (current_price - stop_loss_calc.stop_loss_price) / current_price * 100
                )
                reason += f'，止损距现价{stop_gap_pct:.1f}%'
            # 量能/投票作为附加信息（不影响触发，只影响推送级别描述）
            if vol_ratio is None:
                reason += f'，{self._volume_context(tech_data)}'
                urgency = '重要'
            elif vol_ratio > heavy_vol_thresh:
                reason += f'，放量破位(量比{vol_ratio:.2f})'
                urgency = '紧急'
            elif vol_ratio > 0.8:
                reason += f'，正常量能(量比{vol_ratio:.2f})'
                urgency = '紧急'
            else:
                reason += f'，缩量(量比{vol_ratio:.2f})但硬触发'
                urgency = '重要'  # 缩量破位降一级但仍执行
            if is_bearish:
                reason += f'；{tech_vote}(score={tech_score:+.1f})共振'
            bpats = [p.get('pattern','') for p in kline_patterns if '看跌' in p.get('signal','') or '压力' in p.get('signal','')]
            if bpats:
                reason += '; K线:' + ','.join(bpats)
            signals.append(ExitSignal(
                stock_code=stock_code, stock_name=stock_name, exit_type='破位止损',
                trigger_price=current_price, stop_loss_price=stop_loss_calc.stop_loss_price,
                reason=reason, urgency=urgency, mode_constrained=False,
                sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
            ))
            logger.info(
                '破位止损(硬触发) %s: %.2f≤止损%.2f，量比%.2f %s',
                stock_code, current_price, stop_loss_calc.stop_loss_price,
                vol_ratio, urgency)

        # 2. 上涨衰竭预警
        exhaustion = []  # 冲高止盈信号（RSI超买/上影线/MA5乖离/MA5压制/KDJ钝化/K线看跌）
        weakness = []    # 趋势转弱信号（投票偏空/布林下轨）

        tech = tech_data.get('tech_signals', {}) or {}
        rsi = tech.get('rsi')
        kline_raw = tech_data.get('kline', [])
        vol_ratio = tech_data.get('volume_ratio', 1.0)

        # ── RSI 超买 ──
        if rsi is not None:
            rsi_severe = self._cfg("exit", "exhaustion", "rsi_severe_overbought", default=80)
            rsi_high = self._cfg("exit", "exhaustion", "rsi_overbought", default=70)
            if rsi > rsi_severe:
                exhaustion.append(('strong', f'RSI={rsi:.1f}(严重超买)'))
            elif rsi > rsi_high:
                exhaustion.append(('medium', f'RSI={rsi:.1f}(高位)'))

        # ── RSI 顶背离 ──
        rsi_div_window = self._cfg("exit", "exhaustion", "rsi_divergence_window", default=14)
        rsi_div_thresh = self._cfg("exit", "exhaustion", "rsi_divergence_threshold", default=65)
        if kline_raw and len(kline_raw) >= rsi_div_window:
            highs_hist = [float(k.get("最高", k.get("high", 0))) for k in kline_raw[-rsi_div_window:]]
            if highs_hist and current_price >= max(highs_hist[:-1]) and rsi is not None and rsi < rsi_div_thresh:
                exhaustion.append(('strong', 'RSI顶背离(价量不配合)'))

        # ── 量价背离 ──
        vp_div_window = self._cfg("exit", "exhaustion", "volume_price_div_window", default=10)
        vp_avg_window = self._cfg("exit", "exhaustion", "volume_price_div_avg_window", default=5)
        vol_shrink = self._cfg("exit", "exhaustion", "volume_shrink_ratio", default=0.8)
        if kline_raw and len(kline_raw) >= vp_div_window:
            vols_hist = [float(k.get("成交量", k.get("volume", 0))) for k in kline_raw[-vp_div_window:]]
            prices_hist = [float(k.get("收盘", k.get("close", 0))) for k in kline_raw[-vp_div_window:]]
            if len(vols_hist) >= vp_avg_window and len(prices_hist) >= vp_avg_window:
                vp_avg_vol = sum(vols_hist[:vp_avg_window]) / vp_avg_window
                vol_trend = vp_avg_vol > 0 and vols_hist[-1] < vp_avg_vol * vol_shrink
                if vol_trend and current_price >= max(prices_hist[:-1]):
                    vp_shrink = vols_hist[-1] / vp_avg_vol if vp_avg_vol > 0 else 0.0
                    # A-修复：量价背离的“量缩”口径必须标注（较前N日均量），
                    # 避免与量能组的同期量比/接口量比口径并排输出时自相矛盾（盛合晶微9-18实证）。
                    vp_label = f'量价背离(价新高量缩:较{vp_avg_window}日均量{vp_shrink:.2f}x)'
                    sp_ratio = (
                        (tech_data.get("tech_signals", {}).get("volume_snapshot") or {})
                        .get("same_period_volume_ratio")
                    )
                    try:
                        sp_ratio = float(sp_ratio) if sp_ratio is not None else None
                    except (TypeError, ValueError):
                        sp_ratio = None
                    if sp_ratio is not None and sp_ratio > 1.2:
                        vp_label += f'；另同期量比{sp_ratio:.2f}x为放量口径(两口径并存,冲突时以量能组为准)'
                    exhaustion.append(('strong', vp_label))

        # ── K线形态 ──
        us_window = self._cfg("exit", "exhaustion", "upper_shadow_window", default=3)
        if kline_raw and len(kline_raw) >= us_window:
            upper_shadows = []
            for k in kline_raw[-us_window:]:
                o = float(k.get("开盘", k.get("open", 0)))
                h = float(k.get("最高", k.get("high", 0)))
                l = float(k.get("最低", k.get("low", 0)))
                c = float(k.get("收盘", k.get("close", 0)))
                if o and h and l and c:
                    body = abs(c - o)
                    us = h - max(o, c)
                    if body > 0:
                        upper_shadows.append(us / body)
            us_min_count = self._cfg("exit", "exhaustion", "upper_shadow_min_count", default=2)
            us_strong = self._cfg("exit", "exhaustion", "upper_shadow_strong_ratio", default=1.5)
            us_medium = self._cfg("exit", "exhaustion", "upper_shadow_medium_ratio", default=1.0)
            if len(upper_shadows) >= us_min_count and all(s > us_strong for s in upper_shadows):
                exhaustion.append(('strong', f'连续上影线({len(upper_shadows)}根)'))
            elif len(upper_shadows) >= us_min_count and all(s > us_medium for s in upper_shadows):
                exhaustion.append(('medium', f'连续上影线({len(upper_shadows)}根)'))

            # 放量长上影
            last = kline_raw[-1] if kline_raw else {}
            o = float(last.get("开盘", last.get("open", 0)))
            h = float(last.get("最高", last.get("high", 0)))
            l = float(last.get("最低", last.get("low", 0)))
            c = float(last.get("收盘", last.get("close", 0)))
            if o and h and l and c:
                body = abs(c - o)
                upper_shadow = h - max(o, c)
                lus_body = self._cfg("exit", "exhaustion", "long_upper_shadow_body_ratio", default=3)
                lus_vol = self._cfg("exit", "exhaustion", "long_upper_shadow_volume_ratio", default=1.5)
                us_body_medium = self._cfg("exit", "exhaustion", "upper_shadow_body_ratio_medium", default=2)
                if upper_shadow > body * lus_body and c < o and vol_ratio > lus_vol:
                    exhaustion.append(('strong', '放量长上影(冲高回落)'))
                elif upper_shadow > body * us_body_medium:
                    exhaustion.append(('medium', '上影线(抛压显现)'))

        # ── MACD ──
        # 注意：MACD 死叉已在投票系统"趋势组"算过一次，不再作为独立衰竭信号
        # 避免双重计票导致卖出信号过于敏感
        # 如需启用，在 timing.yaml 设置 exit.exhaustion.macd_as_exhaustion: true
        macd_as_exhaustion = self._cfg("exit", "exhaustion", "macd_as_exhaustion", default=False)
        macd = tech.get('macd', {})
        if macd_as_exhaustion and macd:
            dif = macd.get('dif', 0)
            dea = macd.get('dea', 0)
            if dif < 0 and dif < dea:
                exhaustion.append(('medium', 'MACD死叉+转负'))
            elif dif < dea:
                exhaustion.append(('medium', 'MACD死叉'))

        # ── KDJ ──
        kdj = tech.get('kdj', {})
        if kdj:
            k_val = kdj.get('k', 50)
            d_val = kdj.get('d', 50)
            j_val = kdj.get('j', 50)
            kdj_dc_k = self._cfg("exit", "exhaustion", "kdj_dead_cross_k", default=70)
            kdj_blunt_j = self._cfg("exit", "exhaustion", "kdj_blunt_j", default=100)
            if k_val > kdj_dc_k and k_val < d_val:
                exhaustion.append(('medium', f'KDJ死叉(K={k_val:.0f}<D={d_val:.0f})'))
            elif j_val > kdj_blunt_j:
                exhaustion.append(('medium', f'KDJ钝化(J={j_val:.0f})'))

        # ── MACD负 + KDJ死叉共振 ──
        if macd and kdj:
            dif = macd.get('dif', 0)
            k_val = kdj.get('k', 50)
            d_val = kdj.get('d', 50)
            resonance_k = self._cfg("exit", "exhaustion", "macd_kdj_resonance_k", default=60)
            if dif < 0 and k_val < d_val and k_val > resonance_k:
                exhaustion.append(('strong', 'MACD负+KDJ死叉(顶部共振)'))

        # ── 布林下轨（趋势转弱，不是冲高止盈）──
        boll = tech.get('bollinger', {})
        if boll.get('position') == 'below':
            weakness.append(('medium', '布林下轨(弱势确认)'))

        # ── 投票（趋势转弱，不是冲高止盈）──
        # 修复问题2: 温和偏空不应作为卖出信号，偏空只是 medium，只有强烈看空才是 strong
        if tech_vote == '强烈看空':
            weakness.append(('strong', f'{tech_vote}({tech_score:+.1f})'))
        elif tech_vote == '偏空':
            weakness.append(('medium', f'{tech_vote}({tech_score:+.1f})'))
        # 温和偏空和中性不作为卖出信号 — 只是轻微偏空，不构成卖出理由

        # ── 极端超卖共振：只降级技术走弱，不拦截破位止损 ──
        rsi6 = tech.get('rsi6')
        kdj_j = kdj.get('j') if kdj else None
        obv = tech.get('obv') or {}
        oversold_tags = []
        try:
            rsi6_oversold = rsi6 is not None and float(rsi6) <= self._cfg(
                "exit", "oversold", "rsi6_min", default=25.0
            )
            if rsi6_oversold:
                oversold_tags.append(f'RSI6超卖({float(rsi6):.1f})')
        except (TypeError, ValueError):
            rsi6_oversold = False
        try:
            kdj_j_oversold = kdj_j is not None and float(kdj_j) <= self._cfg(
                "exit", "oversold", "kdj_j_max", default=5.0
            )
            if kdj_j_oversold:
                oversold_tags.append(f'KDJ-J超卖({float(kdj_j):.1f})')
        except (TypeError, ValueError):
            kdj_j_oversold = False
        boll_below = boll.get('position') == 'below'
        if boll_below:
            oversold_tags.append('布林下轨')
        if obv.get('direction') == '下行':
            oversold_tags.append('OBV下行')
        oversold_resonance = bool(rsi6_oversold and kdj_j_oversold and boll_below)

        # ── 均线乖离（冲高止盈：价格偏离均线过远）──
        ma5 = tech_data.get('ma5')
        if ma5 and ma5 > 0:
            bias = (current_price - ma5) / ma5
            ma5_overheat = self._cfg("exit", "exhaustion", "ma5_bias_overheat", default=0.08)
            if bias > ma5_overheat:
                exhaustion.append(('medium', f'MA5乖离{bias*100:.1f}%(过热)'))

        # ── MA5 压制（核心出场信号：沿MA5趋势被破坏）──
        # C2 整改（2026-07-22）：1% 固定阈值 → ATR 自适应 + attack 模式放宽
        # ----------------------------------------------------------------
        # 与进场逻辑对称：进场要求"站上MA5"(price>ma5)，出场就是"跌破MA5"
        # 这是"沿MA5做"交易体系的核心：站上MA5买入，跌破MA5卖出
        #
        # 触发条件（全部满足）：
        # 1. 多头排列（ma5 > ma10 > ma20）— 确保在上升趋势中，震荡市不触发
        # 2. MA5 仍上升（ma5_prev > ma5_prev2）— 趋势还没转头
        # 3. 当前价跌破 MA5 超过阈值 — 阈值由 ATR 自适应计算
        #
        # C2 改动原因（v1 问题：attack 期间被 MA5 压制卖飞主升浪）：
        #   - 1% 固定阈值在 attack 模式下太敏感，日内波动即触发
        #   - ATR 自适应：高波动股票阈值放宽，低波动收紧
        #   - attack 模式阈值 ×1.5（主升浪期间容忍更大回撤）
        #   - defend 模式阈值 ×1.0（正常）
        #   - retreat 模式阈值 ×0.8（破位即走，更敏感）
        ma5_pressure_signal = None
        ma10 = tech_data.get('ma10')
        ma20 = tech_data.get('ma20')
        ma5_prev = tech_data.get('ma5_prev')
        ma5_prev2 = tech_data.get('ma5_prev2')
        if ma5 and ma5 > 0 and ma10 and ma20 and current_price > 0 and ma5_prev and ma5_prev2:
            is_bullish_alignment = ma5 > ma10 > ma20  # 多头排列
            ma5_rising = ma5_prev > ma5_prev2          # MA5 仍上升
            ma5_break_pct = (ma5 - current_price) / ma5

            # C2 v2: ATR 自适应阈值（内联计算，不依赖 tech_data.atr 字段）
            # ATR = 过去14日 True Range 均值
            # True Range = max(H-L, |H-前收|, |L-前收|)
            # 阈值 = max(1.0%, ATR/现价)  最低 1% 防毛刺
            kline_for_atr = tech_data.get('kline', [])
            atr_period = 14
            atr = 0.0
            if len(kline_for_atr) >= atr_period + 1:
                highs = [float(k.get('最高', k.get('high', 0))) for k in kline_for_atr[-(atr_period+1):]]
                lows = [float(k.get('最低', k.get('low', 0))) for k in kline_for_atr[-(atr_period+1):]]
                closes_hist = [float(k.get('收盘', k.get('close', 0))) for k in kline_for_atr[-(atr_period+1):]]
                tr_list = []
                for i in range(1, len(highs)):
                    tr = max(highs[i] - lows[i],
                             abs(highs[i] - closes_hist[i-1]),
                             abs(lows[i] - closes_hist[i-1]))
                    tr_list.append(tr)
                atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
            atr_ratio = atr / current_price if current_price > 0 else 0
            base_threshold = max(0.01, atr_ratio)  # 最低 1%

            # C2: 模式自适应倍数
            # attack ×1.5（主升浪容忍更大回撤）
            # defend ×1.0（正常）
            # retreat ×0.8（破位即走，更敏感）
            if market_mode == 'attack':
                mode_multiplier = 1.5
            elif market_mode == 'retreat':
                mode_multiplier = 0.8
            else:
                mode_multiplier = 1.0

            threshold = base_threshold * mode_multiplier
            price_below_ma5 = ma5_break_pct > threshold

            if is_bullish_alignment and ma5_rising and price_below_ma5:
                ma5_pressure_signal = f'MA5压制(价{current_price:.2f}<MA5:{ma5:.2f},跌{ma5_break_pct*100:.1f}%,阈值{threshold*100:.1f}%,ATR{atr_ratio*100:.1f}%,{market_mode}×{mode_multiplier})'

        # ── K线看跌形态 ──
        for p in kline_patterns:
            sig = p.get('signal', '')
            if '看跌' in sig:
                pat = p.get('pattern', '')
                if '吞没' in pat or '乌云' in pat:
                    exhaustion.append(('medium', f'K线:{pat}'))

        # ============================================================
        # 【四】Block 6: 策略配对 W（兑现离场）—— 价位硬触发 + 确认追强动能耗尽
        # 买卖敏感度对称：进场精确到 0.1% 挂单，出场同样在价位上触发，
        # 不再等 8~10% 外加四重投票。
        # ============================================================
        if paired_position:
            w_low = float(paired_position.get("paired_w_low") or 0)
            w_high = float(paired_position.get("paired_w_high") or 0)
            if w_low > 0 and current_price >= w_low:
                kline_raw_pw = tech_data.get('kline', [])
                recent_5d_low = (
                    min(float(k.get('最低', k.get('low', 999999))) for k in kline_raw_pw[-5:])
                    if len(kline_raw_pw) >= 5 else current_price
                )
                trailing = round(max(recent_5d_low * 1.02, w_low), 2)  # trailing跟随近5日低
                hit_high = w_high > 0 and current_price >= w_high
                action = "清仓兑现" if hit_high else "减半+trailing跟随"
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='策略兑现', trigger_price=current_price,
                    stop_loss_price=min(trailing, current_price),
                    reason=(f'[{paired_strategy}] 兑现条件W触发: 现价{current_price:.2f}'
                            f'触及兑现位{w_low:.2f}'
                            + (f'~{w_high:.2f}' if w_high > w_low else '')
                            + f'，{action}；trailing {trailing:.2f}（价位触发，非投票门）'),
                    urgency='紧急' if hit_high else '重要', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                    source='paired', paired_strategy=paired_strategy,
                ))
            elif paired_strategy == '确认追强' and exhaustion:
                strong_ex = sum(1 for lvl, _ in exhaustion if lvl == 'strong')
                if strong_ex >= 1:
                    labels = '; '.join(lbl for _, lbl in exhaustion)
                    signals.append(ExitSignal(
                        stock_code=stock_code, stock_name=stock_name,
                        exit_type='策略兑现', trigger_price=current_price,
                        stop_loss_price=stop_loss_calc.stop_loss_price,
                        reason=f'[确认追强] 兑现条件W(动能耗尽): {labels}——提前分批兑现',
                        urgency='重要', mode_constrained=False,
                        sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                        source='paired', paired_strategy=paired_strategy,
                    ))


        # === 推送 ===
        # 三类信号分别推送：冲高止盈 / MA5压制 / 技术走弱
        strong_min = self._cfg("exit", "exhaustion", "strong_signal_min_count", default=2)

        # 1. 冲高止盈（RSI超买/上影线/MA5乖离过热/KDJ钝化/K线看跌）
        if exhaustion:
            strong = sum(1 for s in exhaustion if s[0] == 'strong')
            if strong >= strong_min:
                labels = [f'[{lvl}]{lbl}' for lvl, lbl in exhaustion]
                reason = '；'.join(labels)
                urgency = '重要' if strong >= 1 else '观察'
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='冲高止盈', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=reason, urgency=urgency, mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))

        # 2. MA5压制 — C2分批trailing（2026-07-25 整改）
        # 原：触发即全卖（60.7%卖飞率）
        # 新：触发时输出"减半+trailing"信号，trailing stop = max(近5日最低, MA10)
        if ma5_pressure_signal:
            kline_raw_c2 = tech_data.get('kline', [])
            recent_5d_low = min([float(k.get('最低', k.get('low', 999999))) for k in kline_raw_c2[-5:]]) if len(kline_raw_c2) >= 5 else current_price
            trailing_stop = max(recent_5d_low, ma10) if ma10 else recent_5d_low
            trailing_stop = min(trailing_stop, current_price)
            signals.append(ExitSignal(
                stock_code=stock_code, stock_name=stock_name,
                exit_type='MA5压制', trigger_price=current_price,
                stop_loss_price=trailing_stop,
                reason=ma5_pressure_signal + ' | 减半+trailing(' + f'{trailing_stop:.2f}' + ')',
                urgency='重要', mode_constrained=False,
                sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
            ))

        # C3 新增四条卖出规则（2026-07-25 整改）
        # 注：C3 规则降级为"观察级"（只log不触发卖出），避免过度交易
        # 大V原意这些是"考虑级"信号，实盘由人工判断是否执行
        # 回测引擎不执行 C3 规则，仅 MA5压制/破位止损/冲高止盈/技术走弱 触发卖出
        kline_raw_c3 = tech_data.get('kline', [])
        c3_observe_only = self._cfg("exit", "c3", "observe_only", default=True)  # C3 规则默认只观察
        if len(kline_raw_c3) >= 2:
            today_k = kline_raw_c3[-1]; prev_k = kline_raw_c3[-2]
            today_open = float(today_k.get('开盘', today_k.get('open', 0)))
            prev_close = float(prev_k.get('收盘', prev_k.get('close', 0)))
            today_close = float(today_k.get('收盘', today_k.get('close', 0)))
            today_high = float(today_k.get('最高', today_k.get('high', 0)))
            prev_high = float(prev_k.get('最高', prev_k.get('high', 0)))
            prev_open = float(prev_k.get('开盘', prev_k.get('open', 0)))

            # C3-1. 异常高开卖：前日跌 + 今日高开>1.5%
            if prev_open > 0 and prev_close > 0:
                open_gap_pct = (today_open - prev_close) / prev_close * 100
                prev_chg = (prev_close - prev_open) / prev_open * 100
                if not c3_observe_only and prev_chg < -1 and open_gap_pct > 1.5:
                    signals.append(ExitSignal(
                        stock_code=stock_code, stock_name=stock_name,
                        exit_type='异常高开', trigger_price=current_price,
                        stop_loss_price=stop_loss_calc.stop_loss_price,
                        reason=f'异常高开{open_gap_pct:.1f}%(前日跌{prev_chg:.1f}%),隔夜方向为跌却被拉高',
                        urgency='重要', mode_constrained=False,
                        sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                    ))

            # C3-2. 反包前天高点=卖点：今日最高>前日最高且收盘回落
            if not c3_observe_only and prev_high > 0 and today_high > prev_high and today_close < today_high * 0.99:
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='反包前高', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=f'今日高{today_high:.2f}>前高{prev_high:.2f},收盘{today_close:.2f}回落',
                    urgency='观察', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))

            # C3-3. 开盘直接5日压制=卖点：开盘<MA5且全天未站上
            if not c3_observe_only and ma5 and ma5 > 0 and today_open < ma5 and today_close < ma5:
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='开盘5日压制', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=f'开盘{today_open:.2f}<MA5:{ma5:.2f},全天未站上(收{today_close:.2f})',
                    urgency='重要', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))

        # C3-4. 破10日取消关注：收盘跌破MA10（且近5日有>=3天在MA10上方）
        if not c3_observe_only and ma10 and ma10 > 0 and current_price < ma10 and len(kline_raw_c3) >= 5:
            above_count = sum(1 for k in kline_raw_c3[-6:-1] if float(k.get('收盘', k.get('close', 0))) > ma10)
            if above_count >= 3:
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='破10日线', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=f'收盘{current_price:.2f}跌破MA10:{ma10:.2f},取消关注',
                    urgency='观察', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))

        # 3. 技术走弱（投票强烈看空/偏空/布林下轨）
        # 【P0-3】卖出条件分级 OR：止损类（价格破Z线、技术走弱）任一即出，
        # 不容商量；止盈类（冲高止盈、MA5压制）保持原有计票。
        # 原阈值 strong≥1 或 medium≥3 近乎永不开启（两天 30+ 次检查零触发，
        # 汇成真空 -6% 无响应）→ 分级 OR 下技术走弱 medium≥2 即出（默认）。
        if weakness:
            strong = sum(1 for s in weakness if s[0] == 'strong')
            medium = sum(1 for s in weakness if s[0] == 'medium')
            graded_or_enabled = bool(
                self._cfg("exit", "graded_or", "enabled", default=True)
            )
            graded_medium_min = int(
                self._cfg("exit", "graded_or", "weakness_medium_min", default=2)
            )
            if graded_or_enabled:
                weak_trigger = strong >= 1 or medium >= graded_medium_min
            else:
                weak_trigger = strong >= 1 or medium >= 3  # 旧 AND 计票行为
            if weak_trigger and oversold_resonance:
                labels = [f'[{lvl}]{lbl}' for lvl, lbl in weakness]
                reason = '[超卖观察·非执行级] ' + '；'.join(labels)
                if oversold_tags:
                    reason += '；' + '；'.join(oversold_tags)
                reason += f'；{self._volume_context(tech_data)}'
                reason += '；确认:放量站上MA5；失效:跌破布林下轨且MACD绿柱扩大'
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='技术走弱', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=reason, urgency='观察', mode_constrained=True,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))
            elif weak_trigger:
                labels = [f'[{lvl}]{lbl}' for lvl, lbl in weakness]
                reason = '；'.join(labels)
                reason += f'；{self._volume_context(tech_data)}'
                if graded_or_enabled and strong == 0 and medium >= graded_medium_min:
                    reason += (
                        f'——【分级OR·止损类】技术走弱 medium{medium}/{graded_medium_min}'
                        '任一即出，不容商量'
                    )
                urgency = '重要' if strong >= 1 else '重要'
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='技术走弱', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=reason, urgency=urgency, mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))


        # ============================================================
        # 【四】配对优先：已知持仓策略时，旧系统出场信号降级为辅助观察
        # （配对 Z/W 是硬触发；破位止损作为系统安全网保留硬触发）
        # 注：必须放在旧信号全部生成之后（推送段之后）再统一降级。
        # ============================================================
        if paired_position:
            for sig in signals:
                if getattr(sig, 'source', 'system') == 'system' and sig.exit_type != '破位止损':
                    sig.urgency = '观察'
                    sig.reason = f"[辅助观察·非策略配对出场] {sig.reason}"

        # 合并同类 + 追加推导链
        if not signals:
            if not tech_data:
                self._exit_diagnostics[stock_code] = "卖出检查: 数据不足，无法判断"
            else:
                strong_exhaustion = sum(1 for level, _ in exhaustion if level == "strong")
                medium_exhaustion = sum(1 for level, _ in exhaustion if level == "medium")
                strong_weakness = sum(1 for level, _ in weakness if level == "strong")
                medium_weakness = sum(1 for level, _ in weakness if level == "medium")
                parts = [
                    self._risk_stop_text(stock_code, current_price, stop_loss_calc),
                    f"冲高止盈(strong {strong_exhaustion}/2)",
                    "MA5压制(未同时满足多头排列/MA5上升/跌破阈值)",
                    (
                        f"技术走弱(strong {strong_weakness}/1, "
                        f"medium {medium_weakness}/2·分级OR止损类)"
                        if bool(self._cfg("exit", "graded_or", "enabled", default=True))
                        else f"技术走弱(strong {strong_weakness}/1, medium {medium_weakness}/3)"
                    ),
                ]
                self._exit_diagnostics[stock_code] = "卖出检查: " + "; ".join(parts)

        exit_types = list(dict.fromkeys(s.exit_type for s in signals))
        merged: Dict[str, ExitSignal] = {}
        for sig in signals:
            if sig.exit_type not in merged:
                merged[sig.exit_type] = sig
            else:
                existing = merged[sig.exit_type]
                all_reasons = [existing.reason, sig.reason]
                urgency_rank = {"紧急": 0, "重要": 1, "常规": 2}
                if urgency_rank.get(sig.urgency, 99) < urgency_rank.get(existing.urgency, 99):
                    existing.urgency = sig.urgency
                if sig.trigger_price < existing.trigger_price:
                    existing.trigger_price = sig.trigger_price
                existing.reason = "；".join(dict.fromkeys(all_reasons))
                logger.debug("同类合并 %s %s: %s", sig.stock_code, sig.exit_type, sig.reason)

        derivation = self._build_derivation(
            tech_data, market_mode,
            triggered_types=exit_types,
            selected_type="/".join(exit_types) if exit_types else None,
            signal_direction="exit",
            sector_status=sector_status,
        )
        for sig in merged.values():
            active_events = self._lifecycle.get_active_events(stock_code)
            triggered = [e for e in active_events if e.status == "triggered"]
            if triggered:
                sig.event_id = triggered[-1].event_id
            sig.reason += "\n  推导: " + derivation

        return list(merged.values())

    # ============================================================
    # 【四】配对持仓读取 / 【三】事件生命周期评估
    # ============================================================

    def _get_paired_position(self, stock_code: str) -> Optional[Dict]:
        """读取该持仓的入场假说（来源：回执闭环 trade_logs executed 买入行）。

        返回 dict: entry_type / paired_z / paired_w_low / paired_w_high /
        z_reference / entry_price / hypothesis_sentence 等；无持仓返回 None。
        回测模式不读 DB，直接返回 None（回测引擎自管出场）。
        """
        if self._backtest_mode:
            return None
        try:
            from ..feedback.trade_logger import get_trade_logger
            position = get_trade_logger().get_open_position(stock_code)
            return position if position else None
        except Exception as e:
            logger.debug("配对持仓读取失败 %s: %s", stock_code, e)
            return None

    def evaluate_signal_events(
        self,
        stock_code: str,
        current_price: float = 0,
        sector_status: str = "",
    ) -> List[Dict]:
        """【三】评估该股活跃信号事件的状态迁移（失效撤单/过期），返回通知列表。

        由 unified_engine 在出场扫描后调用；返回值只进 batch.event_notices。
        “信号成交”是买单状态迁移，不是卖出指令；“信号作废”是撤单状态。
        """
        try:
            tech_data = self._tech_data_full.get(stock_code) or {}
            kline = tech_data.get("kline") or [] if isinstance(tech_data, dict) else []
            latest_bar = kline[-1] if kline else {}
            day_high = _number_or_none(latest_bar.get("最高", latest_bar.get("high")))
            day_low = _number_or_none(latest_bar.get("最低", latest_bar.get("low")))
            close_price = _number_or_none(
                current_price or latest_bar.get("收盘", latest_bar.get("close"))
            )
            exchange_close = _number_or_none(
                latest_bar.get("收盘", latest_bar.get("close"))
            )
            tech_signals = tech_data.get("tech_signals") or {}
            ma5, ma10, ma20 = tech_data.get("ma5"), tech_data.get("ma10"), tech_data.get("ma20")
            vs_snap = tech_signals.get("volume_snapshot") or {}
            maintenance_check = {
                "date": datetime.now().date().isoformat(),
                # C-修复：维持面板量比必须有口径标注（沃尔德9-18 量比4.69 vs 同期量比1.57/
                # 接口量比2.33 并存无口径）；早盘用同期口径、其余用有效口径，并落库口径名。
                "volume_ratio": tech_data.get("volume_ratio"),
                "volume_ratio_effective": vs_snap.get("volume_ratio_effective"),
                "same_period_volume_ratio": vs_snap.get("same_period_volume_ratio"),
                "is_early_window": bool(vs_snap.get("is_early_window")),
                "volume_ratio_caliber": (
                    vs_snap.get("volume_ratio_caliber")
                    or tech_data.get("volume_ratio_source")
                    or "口径未标注"
                ),
                "same_period_caliber": vs_snap.get("same_period_caliber") or "",
                "ma_alignment": bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20),
                "rsi": tech_signals.get("rsi"),
                "sector_status": sector_status,
            }
            try:
                self._lifecycle.set_maintenance_check(stock_code, maintenance_check)
            except Exception as e:
                logger.debug("维持检查写入失败 %s: %s", stock_code, e)
            return self._lifecycle.evaluate_events(
                stock_code,
                current_price=current_price,
                sector_status=sector_status,
                day_high=day_high,
                day_low=day_low,
                close_price=close_price,
                exchange_close=exchange_close,
                tech_score=tech_signals.get("vote_score"),
                volume_ratio=tech_data.get("volume_ratio"),
                change_pct=tech_data.get("change_pct"),
            )
        except Exception as e:
            logger.debug("事件评估失败 %s: %s", stock_code, e)
            return []

    def lifecycle_status_note(self, stock_code: str, current_price: float = 0) -> str:
        """【三】观察卡用：活跃事件状态（回踩买点是否有效/第几天）。

        【Phase5 回炉】传入当前 z_line_mode：存量事件与新规则双轨
        在状态行可见（“Z按bare_structure(旧版)”），审计者不再误判。
        """
        try:
            current_mode = str(
                self._cfg("hypothesis_gate", "z_line_mode", default="buffered_structure")
            )
            return self._lifecycle.event_status_note(
                stock_code, current_price, current_rule_version=current_mode
            )
        except Exception:
            return ""

    def run_daily_event_unfreeze(
        self, tech_scores: Dict[str, float], today=None,
    ) -> List[Dict]:
        """日终仲裁入口：解冻判定只由批处理驱动，展示层不再二次推断。"""
        return self._lifecycle.run_daily_unfreeze(tech_scores, today=today)

    def _risk_stop_text(self, stock_code: str, current_price: float,
                        stop_loss_calc: StopLossCalc) -> str:
        """风控区双轨：事件结构位Z与当日技术支撑跟踪线同屏。"""
        active_events = self._lifecycle.get_active_events(stock_code)
        event_stops = sorted({
            float(event.stop_loss) for event in active_events
            if event.stop_loss and float(event.stop_loss) > 0
        })
        if not self._backtest_mode and stop_loss_calc.stop_loss_price > 0:
            try:
                from datetime import date
                from ..feedback.signal_ledger import record_tracking_stop
                from ..rules_version import get_rules_version
                log_date = date.today().isoformat()
                for event in active_events:
                    if not event.stop_loss or float(event.stop_loss) <= 0:
                        continue
                    record_tracking_stop(
                        event.event_id,
                        log_date,
                        float(event.stop_loss),
                        float(stop_loss_calc.stop_loss_price),
                        current_price,
                        get_rules_version(),
                    )
            except Exception as e:
                logger.debug("跟踪止损日志写入失败 %s: %s", stock_code, e)
        # P1-12：结构位=突破失败判定基准(breakout_level)，事件止损Z=风险出口，
        # 两个术语分列标注，禁止把跟踪止损标为“结构位”。
        structure_levels = sorted({
            float(event.breakout_level) for event in active_events
            if event.breakout_level and float(event.breakout_level) > 0
        })
        bits = []
        if structure_levels:
            bits.append(
                "结构位:" + "/".join(f"{p:.2f}" for p in structure_levels)
                + "(突破失败判定基准)"
            )
        if event_stops:
            bits.append(
                "事件跟踪止损:" + "/".join(f"{p:.2f}" for p in event_stops)
                + "(风险出口)"
            )
        prefix = " | ".join(bits) if bits else "跟踪止损"
        if stop_loss_calc.stop_loss_price > 0:
            return (
                f"{prefix} | 技术跟踪止损未触发(现价{current_price:.2f}>"
                f"{stop_loss_calc.stop_loss_price:.2f})"
            )
        return f"{prefix} 数据未取到"

    # ============ 止损价计算 ============

    def calculate_stop_loss(self, stock_code: str, tech_data: Optional[Dict] = None) -> StopLossCalc:
        """
        计算止损价
        候选支撑位：MA5 / MA10 / MA20 / 布林下轨
        止损价 = 支撑位 × stop_loss.multiplier
        """
        if tech_data is None:
            tech_data = self._fetch_tech_data(stock_code, "defend")

        current_price = tech_data.get("current_price", 0)
        ma5 = tech_data.get("ma5")
        ma10 = tech_data.get("ma10")
        ma20 = tech_data.get("ma20")

        boll = tech_data.get("tech_signals", {}).get("bollinger", {})
        boll_lower = boll.get("lower") if boll else None

        candidates = []
        if ma5:
            candidates.append({"name": "MA5", "value": ma5})
        if ma10:
            candidates.append({"name": "MA10", "value": ma10})
        if ma20:
            candidates.append({"name": "MA20", "value": ma20})
        if boll_lower:
            candidates.append({"name": "布林下轨", "value": boll_lower})

        # 修复 BUG-E1: 止损价距离过远时回退到固定百分比
        # 当选中的支撑位离当前价超过 max_support_distance（默认 12%）时，
        # 说明所有 MA 都在当前价上方（刚破位），此时用固定百分比兜底
        max_support_distance = self._cfg("stop_loss", "max_support_distance", default=0.12)
        fallback_ratio = self._cfg("stop_loss", "fallback_support_ratio", default=0.92)

        # C1整改: 标记是否走fallback路径（fallback路径止损价不再×0.97）
        is_fallback = False
        if not candidates:
            chosen_support = current_price * fallback_ratio
            is_fallback = True
        else:
            below = [c for c in candidates if c["value"] < current_price]
            if below:
                # 选最高的支撑位（最接近当前价）
                chosen_support = max(below, key=lambda c: c["value"])["value"]
                # 修复 BUG-E1: 如果选中的支撑位距离当前价过远，回退到固定百分比
                if current_price > 0 and (current_price - chosen_support) / current_price > max_support_distance:
                    chosen_support = current_price * fallback_ratio
                    is_fallback = True
            else:
                # 所有 MA 都在当前价上方 → 刚破位，用固定百分比
                chosen_support = current_price * fallback_ratio
                is_fallback = True

        # ATR buffer（修复 BUG-B19: 接入止损价计算，通过 use_atr_buffer 开关控制）
        use_atr_buffer = self._cfg("stop_loss", "use_atr_buffer", default=False)
        atr_min_klines = self._cfg("stop_loss", "atr_min_klines", default=15)
        atr_period = self._cfg("stop_loss", "atr_period", default=14)
        atr_mult = self._cfg("stop_loss", "atr_multiplier", default=2)
        fallback_buffer_ratio = self._cfg("stop_loss", "fallback_buffer_ratio", default=0.02)
        kline = tech_data.get("kline", [])
        buffer = 0.0
        if use_atr_buffer:
            buffer = current_price * fallback_buffer_ratio
            if kline and len(kline) >= atr_min_klines:
                highs = [float(k.get("最高", k.get("high", 0))) for k in kline[-atr_min_klines:]]
                lows  = [float(k.get("最低", k.get("low", 0))) for k in kline[-atr_min_klines:]]
                closes_hist = [float(k.get("收盘", k.get("close", 0))) for k in kline[-atr_min_klines:]]
                if len(highs) >= atr_period and len(lows) >= atr_period and len(closes_hist) >= atr_period:
                    tr_list = []
                    for i in range(1, len(highs)):
                        tr = max(highs[i] - lows[i],
                                 abs(highs[i] - closes_hist[i-1]),
                                 abs(lows[i] - closes_hist[i-1]))
                        tr_list.append(tr)
                    atr = sum(tr_list) / len(tr_list)
                    buffer = atr_mult * atr

        # 止损价 = 支撑位 × multiplier - buffer
        # C1 v3整改: fallback路径用 ATR 自适应止损价（而非固定5%）
        # fallback时 chosen_support = current_price × 0.95（距5%），但V型转折急跌段收盘往往拉回
        # 改为: stop_loss = current_price - ATR×1.0（约1.5-3%，随波动率自适应）
        stop_loss_multiplier = self._cfg("stop_loss", "multiplier", default=0.97)
        if is_fallback:
            # C1 v3: fallback 用 ATR 自适应
            kline_for_atr = tech_data.get('kline', [])
            atr_period = 14
            atr_val = 0.0
            if len(kline_for_atr) >= atr_period + 1:
                highs = [float(k.get('最高', k.get('high', 0))) for k in kline_for_atr[-(atr_period+1):]]
                lows = [float(k.get('最低', k.get('low', 0))) for k in kline_for_atr[-(atr_period+1):]]
                closes_hist = [float(k.get('收盘', k.get('close', 0))) for k in kline_for_atr[-(atr_period+1):]]
                tr_list = []
                for i in range(1, len(highs)):
                    tr = max(highs[i] - lows[i],
                             abs(highs[i] - closes_hist[i-1]),
                             abs(lows[i] - closes_hist[i-1]))
                    tr_list.append(tr)
                atr_val = sum(tr_list) / len(tr_list) if tr_list else 0.0
            if atr_val > 0 and current_price > 0:
                # ATR止损：现价 - 1×ATR（约1.5-3%距离）
                stop_loss_price = current_price - atr_val
                # 保底：不低于现价×0.90（最多10%止损）
                floor = current_price * 0.90
                stop_loss_price = max(stop_loss_price, floor)
            else:
                stop_loss_price = chosen_support  # ATR不足时退回原fallback
        else:
            stop_loss_price = chosen_support * stop_loss_multiplier - buffer

        prev_high = tech_data.get("prev_high")
        resistance = prev_high or 0
        recent_high = tech_data.get("recent_high", 0)
        if recent_high > resistance:
            resistance = recent_high

        # C4: 阶梯止损（帖33：59破了→58→52，每级对应减仓）
        ladder = None
        try:
            below_c4 = [c for c in candidates if c["value"] < current_price]
            if len(below_c4) >= 2:
                sorted_supports = sorted([c["value"] for c in below_c4], reverse=True)
                ladder = []
                reduce_ratios = [0.30, 0.30, 0.40]
                for i, sp in enumerate(sorted_supports[:3]):
                    ratio = reduce_ratios[i] if i < len(reduce_ratios) else 0.40
                    ladder.append({'support': sp, 'stop_loss_price': sp * stop_loss_multiplier, 'reduce_ratio': ratio, 'level': i + 1})
        except Exception as e:
            logger.debug("非关键异常: %s", e)

        return StopLossCalc(
            stock_code=stock_code,
            current_price=current_price,
            support_candidates=candidates,
            chosen_support=chosen_support,
            ladder=ladder,
            stop_loss_price=stop_loss_price,
            resistance=resistance,
        )

    def set_signal_context(self, market_mode: str = "defend", sector_status: str = ""):
        """设置在信号分析期间的上下文"""
        self._market_context = market_mode
        self._sector_context = sector_status

    def _build_derivation(self, tech_data, market_mode="defend",
                          triggered_types=None, selected_type=None, confidence="中",
                          signal_direction="entry", sector_status=""):
        """构建策略推导链（修复 BUG-B16: 优先用传入的 sector_status，回退到 _sector_context）"""
        lines = []
        mode_names = {"attack": "🟢进攻", "defend": "🟡防守", "retreat": "🔴撤退"}
        mode_cn = mode_names.get(market_mode, market_mode)
        is_exit = (signal_direction == "exit")

        # 修复 BUG-B16: 优先用参数传入的 sector_status
        sector = sector_status or getattr(self, '_sector_context', '') or ''

        # ① 策略环境
        if is_exit:
            if sector == "retreating":
                sector_warn = " | 板块:退潮(恶化预警)"
            elif sector:
                sector_warn = f" | 板块:{sector}"
            else:
                sector_warn = ""
            lines.append(f"①环境: {mode_cn}{sector_warn}")
        else:
            sector_note = f" | 板块:{sector}" if sector else ""
            if market_mode == "attack":
                strategy_note = "全部策略可用"
            elif market_mode == "defend":
                # 【Phase5 回炉】与环境闸门栏同步（旧文案“禁用:确认追强”与
                # 闸门行“追强降仓可用(三重门)”矛盾——两处表述相反时，
                # 执行以谁为准？审计视角这是 P1 级问题）。
                strategy_note = "可用:恐慌抄底/套利低吸/价量突破/趋势延续 | 追强降仓需三重门"
            else:
                strategy_note = "仅恐慌抄底可用"
            lines.append(f"①策略: {mode_cn} → {strategy_note}{sector_note}")

        # ② 技术综合
        tech = tech_data.get("tech_signals", {})
        vote_score = tech.get("vote_score", 0) if isinstance(tech, dict) else 0

        vote_bullish = self._cfg("derivation", "vote_bullish_threshold", default=1.0)
        vote_bearish = self._cfg("derivation", "vote_bearish_threshold", default=-1.0)
        if vote_score > vote_bullish:
            vote_label = f"投票偏多↑({vote_score:+.1f})"
            vote_dir = 1
        elif vote_score < vote_bearish:
            vote_label = f"投票偏空↓({vote_score:+.1f})"
            vote_dir = -1
        elif vote_score > 0:
            vote_label = f"投票温和偏多({vote_score:+.1f})"
            vote_dir = 0.5
        elif vote_score < 0:
            vote_label = f"投票温和偏空({vote_score:+.1f})"
            vote_dir = -0.5
        else:
            vote_label = "投票中性"
            vote_dir = 0

        contradictions = []

        ma5, ma10, ma20 = tech_data.get("ma5"), tech_data.get("ma10"), tech_data.get("ma20")
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                ma_dir = 1
            elif ma5 < ma10 < ma20:
                ma_dir = -1
            else:
                ma_dir = 0
            if vote_dir != 0 and ma_dir != 0 and (vote_dir > 0) != (ma_dir > 0):
                contradictions.append("MA空头⚠️" if ma_dir < 0 else "MA多头(与投票反向)")

        patterns = tech_data.get("kline_pattern", [])
        if patterns:
            bull_count = sum(1 for p in patterns if "看涨" in p.get("signal", ""))
            bear_count = sum(1 for p in patterns if "看跌" in p.get("signal", ""))
            if bull_count > bear_count:
                kline_dir = 1
                kline_label = f"K线看涨({bull_count}票)"
            elif bear_count > bull_count:
                kline_dir = -1
                kline_label = f"K线看跌({bear_count}票)"
            else:
                kline_dir = 0
                kline_label = f"K线分歧(涨{bull_count}vs跌{bear_count})"
            if vote_dir != 0 and kline_dir != 0 and (vote_dir > 0) != (kline_dir > 0):
                contradictions.append(kline_label)

        if isinstance(tech, dict):
            rsi = tech.get("rsi")
            if rsi is not None:
                rsi_high_con = self._cfg("derivation", "rsi_high_contradiction", default=65)
                rsi_low_con = self._cfg("derivation", "rsi_low_contradiction", default=35)
                if rsi > rsi_high_con and vote_dir < 0:
                    contradictions.append(f"RSI偏高{rsi:.0f}(与投票反向)")
                elif rsi < rsi_low_con and vote_dir > 0:
                    contradictions.append(f"RSI偏低{rsi:.0f}(与投票反向)")

        if contradictions:
            lines.append(f"②技术: {vote_label} | ⚠️矛盾: {' | '.join(contradictions)}")
        else:
            lines.append(f"②技术: {vote_label}")

        # ②.5 机构持仓打分（4 数据源投票，中等权重 25%）
        # 简单投票制：北向/龙虎榜/主力/股东户数 各 1 票
        # API 失败默认 0 票（中性），不影响其他维度
        inst = tech_data.get("institutional_holding")
        if isinstance(inst, dict):
            inst_score = inst.get("vote_score", 0)
            inst_label = inst.get("vote_label", "资金中性")
            inst_bull = inst.get("bullish_count", 0)
            inst_bear = inst.get("bearish_count", 0)
            stale_mark = "📅" if inst.get("stale") else ""
            # 构造投票详情简述
            votes_detail = inst.get("votes", {})
            vote_parts = []
            for src_name, src_data in votes_detail.items():
                v = src_data.get("vote", 0)
                if v > 0:
                    vote_parts.append(f"{src_name}↑")
                elif v < 0:
                    vote_parts.append(f"{src_name}↓")
            vote_summary = "/".join(vote_parts) if vote_parts else "全中性"
            lines.append(
                f"②.5资金: {inst_label}({inst_score:+d}票,看多{inst_bull}/看空{inst_bear}) "
                f"[{vote_summary}]{stale_mark}"
            )

            # 【Phase3】分析师共识旁路展示（不混票：资金是节奏、研报是方向）
            analyst = inst.get("analyst_consensus")
            if isinstance(analyst, dict) and analyst.get("total"):
                lines.append(
                    f"②.6研报: 共识{analyst.get('consensus_label', '无数据')}"
                    f"(买入{analyst.get('buy', 0)}/增持{analyst.get('outperform', 0)}"
                    f"/中性{analyst.get('neutral', 0)}/减持{analyst.get('reduce', 0)}，"
                    f"近90天{analyst.get('total', 0)}份)"
                )
                if inst.get("flow_analyst_conflict"):
                    lines.append("   ↳ ⚠️资金与研报共识反向(双标签呈现，禁止单标签定性)")

            # 资金与技术面共振/矛盾判断
            if inst_score >= 2 and vote_dir > 0:
                lines.append("   ↳ 资金与技术共振看多✅")
            elif inst_score <= -2 and vote_dir < 0:
                lines.append("   ↳ 资金与技术共振看空⚠️")
            elif inst_score >= 2 and vote_dir < 0:
                lines.append("   ↳ ⚠️资金看多但技术看空(分歧)")
            elif inst_score <= -2 and vote_dir > 0:
                lines.append("   ↳ ⚠️资金看空但技术看多(分歧)")

        # ③ 信号触发
        if triggered_types:
            lines.append(f"③触发: {'/'.join(triggered_types)}")
        else:
            lines.append("③触发: 无明确信号")

        # ④ 决策
        if selected_type:
            lines.append(f"④决策: 选取{selected_type} | 置信度:{confidence}")

        return "\n  ".join(lines)

    @staticmethod
    def _compute_prior_high(highs: List, window: int):
        """【Phase5 回炉】前期高点：近 N 日最高价，剔除当日。

        "创新高/突破"判定的正确锚（含当日高点的旧口径实际测度的是
        "收盘接近日内最高"——蘅东光 9/7 盘中 555.10 创历史新高、
        收盘 549.50 回落 1.0%，被 0.99 容差误拦）。
        """
        if not highs:
            return None
        prior_window = (
            highs[-(window + 1):-1] if len(highs) >= window + 1 else highs[:-1]
        )
        return max(prior_window) if prior_window else None

    def _get_realtime_price(self, stock_code: str) -> Optional[Dict]:
        """获取实时价"""
        # 回测模式：从注入的 K 线取最后收盘价
        if self._backtest_mode:
            kline = self._backtest_kline.get(stock_code, [])
            if kline and len(kline) >= 2:
                last = kline[-1]
                prev = kline[-2]
                # 修复 BUG-B2: 兼容中文键
                close = float(last.get("收盘", last.get("close", 0)) or 0)
                prev_close = float(prev.get("收盘", prev.get("close", 0)) or 0)
                change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                return {"price": close, "change_pct": change_pct}
            return None
        # 实盘模式
        try:
            from ..data_layer.stock_data import batch_get_realtime_quotes
            fresh = batch_get_realtime_quotes([stock_code])
            if stock_code in fresh and fresh.get(stock_code):
                q = fresh[stock_code]
                return {"price": q.get("current_price", 0), "change_pct": q.get("change_pct", 0),
                        "volume_ratio": q.get("volume_ratio", 0), "quote": q}
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        return None

    @staticmethod
    def _sync_last_kline_with_realtime(kline: List[Dict], quote: Optional[Dict]) -> List[Dict]:
        """把当前交易日的实时 OHLCV 合入历史K线，避免现价与指标数据源错位。"""
        if not kline or not quote:
            return kline

        current = float(quote.get("current_price", 0) or 0)
        if current <= 0:
            return kline

        stamp = str(quote.get("timestamp", "") or "")
        if stamp.isdigit() and len(stamp) >= 8:
            quote_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        else:
            quote_date = datetime.now().strftime("%Y-%m-%d")

        last = kline[-1]
        last_date = str(last.get("date", last.get("日期", "")))[:10]
        open_price = float(quote.get("today_open", 0) or 0)
        high_price = float(quote.get("today_high", 0) or 0)
        low_price = float(quote.get("today_low", 0) or 0)
        quote_volume = float(quote.get("volume", 0) or 0)
        quote_amount = float(quote.get("amount", 0) or 0)
        quote_reference_price = float(quote.get("prev_close", 0) or current)
        quote_volume = quote_volume * _volume_share_factor(
            quote_volume, quote_amount, quote_reference_price
        )

        # Historical sources may report lots while realtime volume is shares.
        # Convert every historical bar to shares before adding the intraday bar.
        reference = kline[-2] if len(kline) >= 2 else kline[-1]
        history_factor = 1
        for item in reversed(kline[:-1]):
            reference_volume = float(item.get("volume", item.get("成交量", 0)) or 0)
            reference_amount = float(item.get("amount", item.get("成交额", 0)) or 0)
            reference_close = float(item.get("close", item.get("收盘", 0)) or 0)
            history_factor = _volume_share_factor(
                reference_volume, reference_amount, reference_close
            )
            if reference_volume > 0 and reference_amount > 0:
                break
        if history_factor == 100:
            for item in kline:
                historical_volume = float(item.get("volume", item.get("成交量", 0)) or 0)
                item["volume"] = historical_volume * 100
                item["成交量"] = historical_volume * 100

        volume = quote_volume
        reference_close = float(reference.get("close", reference.get("收盘", 0)) or 0)

        if last_date == quote_date:
            updates = {"close": current, "收盘": current}
            if open_price > 0:
                updates.update({"open": open_price, "开盘": open_price})
            if high_price > 0:
                high_price = max(high_price, current)
                updates.update({"high": high_price, "最高": high_price})
            if low_price > 0:
                low_price = min(low_price, current)
                updates.update({"low": low_price, "最低": low_price})
            if volume > 0:
                updates.update({"volume": volume, "成交量": volume})
            turnover = float(quote.get("turnover_rate", 0) or 0)
            if turnover > 0:
                updates.update({"turnover_rate": turnover, "换手率": turnover})
            last.update(updates)
        elif last_date and last_date < quote_date and open_price > 0 and volume > 0:
            bar = {
                "date": quote_date, "开盘": open_price,
                "high": max(high_price, current), "最高": max(high_price, current),
                "low": min(low_price, current), "最低": min(low_price, current),
                "close": current, "收盘": current,
                "volume": volume, "成交量": volume,
            }
            if open_price > 0:
                bar["open"] = open_price
            turnover = float(quote.get("turnover_rate", 0) or 0)
            if turnover > 0:
                bar.update({"turnover_rate": turnover, "换手率": turnover})
            kline.append(bar)

        return kline

    def _fetch_tech_data(self, stock_code: str, market_mode: str = "defend") -> Dict[str, Any]:
        """获取技术分析数据（含多级均线 + 盘中实时信号）"""
        data = {}

        # 从预取缓存获取实时价
        realtime = self._get_realtime_price(stock_code)
        if realtime:
            data["current_price"] = realtime.get("price", 0)
            data["change_pct"] = realtime.get("change_pct", 0)
            if realtime.get("quote", {}).get("turnover_rate"):
                data["turnover_rate"] = realtime["quote"]["turnover_rate"]
            # 【三】今日开盘价（事件诞生收阳判定用）
            if realtime.get("quote", {}).get("today_open"):
                data["today_open"] = realtime["quote"]["today_open"]
            # 量比：优先用行情接口返回字段（与同花顺/腾讯一致），
            # 不用 K 线均量近似值；无接口数据（回测/停牌/接口缺失）时才用 K 线兜底
            if realtime.get("volume_ratio"):
                data["volume_ratio"] = realtime["volume_ratio"]
                data["volume_ratio_source"] = "行情接口"
                data["volume_ratio_raw"] = realtime["volume_ratio"]
            quote = realtime.get("quote") or {}
            stamp = str(quote.get("timestamp", "") or "")
            if stamp.isdigit() and len(stamp) >= 14:
                data["volume_ratio_sample_time"] = stamp
                data["data_date"] = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
            if quote.get("outer_volume") is not None:
                data["outer_volume"] = quote["outer_volume"]
            if quote.get("inner_volume") is not None:
                data["inner_volume"] = quote["inner_volume"]

        # 获取 K 线
        kline = None
        if self._backtest_mode:
            # 回测模式：从注入的 K 线取
            kline = self._backtest_kline.get(stock_code, [])
        else:
            # 实盘模式：优先缓存，否则拉 akshare
            cached_kline = self._tech_cache.get(stock_code)
            if cached_kline is not None:
                kline = cached_kline
            else:
                hist_calendar_days = self._cfg("prefetch", "hist_calendar_days", default=240)
                start_date = (datetime.now() - timedelta(days=hist_calendar_days)).strftime("%Y%m%d")
                hist_result = self._akshare.get_stock_hist(stock_code, start_date=start_date)
                if hist_result.success and hist_result.data:
                    kline = hist_result.data

        if not self._backtest_mode and kline:
            kline = self._sync_last_kline_with_realtime(kline, realtime.get("quote") if realtime else None)
            last_bar_date = str(kline[-1].get("date", kline[-1].get("日期", "")))[:10]
            data["kline_includes_today"] = bool(
                data.get("data_date") and last_bar_date == data["data_date"]
            )

        if kline:
            data["kline"] = kline
            last_turnover = kline[-1].get("turnover_rate", kline[-1].get("换手率"))
            if data.get("turnover_rate") is None and last_turnover is not None:
                data["turnover_rate"] = last_turnover
            min_klines = self._cfg("tech_data", "min_klines_for_indicator", default=20)
            if kline and len(kline) >= min_klines:
                try:
                    closes = [float(k.get("收盘", k.get("close", 0))) for k in kline]
                    highs = [float(k.get("最高", k.get("high", 0))) for k in kline]
                    lows = [float(k.get("最低", k.get("low", 0))) for k in kline]
                    opens = [float(k.get("开盘", k.get("open", 0))) for k in kline]
                    volumes = [float(k.get("成交量", k.get("volume", 0))) for k in kline]

                    # 修复 BUG-B1: change_pct=0 时也需重新计算（原逻辑只检查 not in）
                    if "current_price" not in data or not data["current_price"]:
                        data["current_price"] = closes[-1]
                    if not data.get("change_pct"):
                        data["change_pct"] = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 and closes[-2] > 0 else 0
                    data["ma5"] = sum(closes[-5:]) / 5
                    data["ma10"] = sum(closes[-10:]) / 10
                    data["ma20"] = sum(closes[-20:]) / 20
                    # MA5 前值（昨日/前日 MA5，用于判断 MA5 是否上升）
                    if len(closes) >= 6:
                        data["ma5_prev"] = sum(closes[-6:-1]) / 5   # 昨日 MA5
                    if len(closes) >= 7:
                        data["ma5_prev2"] = sum(closes[-7:-2]) / 5  # 前日 MA5

                    ma25_window = self._cfg("tech_data", "ma25_window", default=25)
                    ma25_prev_window = self._cfg("tech_data", "ma25_prev_window", default=26)
                    if len(closes) >= ma25_window:
                        data["ma25"] = sum(closes[-ma25_window:]) / ma25_window
                        data["ma25_prev"] = sum(closes[-ma25_prev_window:-1]) / ma25_window if len(closes) >= ma25_prev_window else data["ma25"]
                        data["prev_close"] = closes[-2] if len(closes) >= 2 else closes[-1]

                    ma60_window = self._cfg("tech_data", "ma60_window", default=60)
                    if len(closes) >= ma60_window:
                        data["ma60"] = sum(closes[-ma60_window:]) / ma60_window

                    ma120_window = self._cfg("tech_data", "ma120_window", default=120)
                    if len(closes) >= ma120_window:
                        data["ma120"] = sum(closes[-ma120_window:]) / ma120_window

                    vol_ma60_window = self._cfg("tech_data", "volume_ma60_window", default=60)
                    if len(volumes) >= vol_ma60_window:
                        # 盘中最后一根已是今日累计量，60日基准必须剔除今日，否则放量自我稀释。
                        if data.get("kline_includes_today") and len(volumes) > vol_ma60_window:
                            data["volume_ma60"] = (
                                sum(volumes[-(vol_ma60_window + 1):-1]) / vol_ma60_window
                            )
                        else:
                            data["volume_ma60"] = sum(volumes[-vol_ma60_window:]) / vol_ma60_window
                        data["today_volume"] = volumes[-1]

                    extreme_window = self._cfg("tech_data", "recent_extreme_window", default=20)
                    data["prev_low"] = min(lows[-extreme_window:]) if len(lows) >= extreme_window else (lows[-1] if lows else None)
                    # 【Phase5 回炉】prev_high 语义修正：真昨日高点（旧实现是当日高点，名不副实）。
                    # calculate_stop_loss 的 resistance 用途由 recent_high 覆盖，不受影响。
                    data["prev_high"] = highs[-2] if len(highs) >= 2 else None
                    data["recent_high"] = max(highs[-extreme_window:]) if len(highs) >= extreme_window else max(highs)
                    # 【Phase5 回炉】prior_high：近 N 日高，剔除当日——“创新高/突破”判定的
                    # 正确锚。验收实证（9/8 蘅东光）：盘中 555.10 创历史新高、收盘 549.50
                    # 回落 1.0%，旧口径（含当日高点）判 current≥recent_high×0.99
                    # 即 549.50≥549.55 差 0.05 元失败——“创新高”被错误实现成
                    # “收盘接近日内最高”，在它最该放行的标的上失效。
                    # prior_high=462.30（9/4 涨停价）时 549.50 稳过。
                    data["prior_high"] = self._compute_prior_high(highs, extreme_window)

                    # 收盘量比（当日量/前5日均量）：历史K线无"量比"字段，但收盘时标准量比数学上
                    # 就等于当日量/前5日均量（实测 300843: 0.9325 vs 实时量比 0.93，误差可忽略）
                    vol_ratio_window = self._cfg("tech_data", "volume_ratio_avg_window", default=6)
                    if "volume_ratio" not in data and len(volumes) >= vol_ratio_window:
                        avg_vol = sum(volumes[-vol_ratio_window:-1]) / (vol_ratio_window - 1)
                        data["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

                    # 缩量回踩MA5/MA10（设计问题5: 放宽条件）
                    # 原条件：量比<0.8 + 乖离±1% + 今日下跌 → 几乎不触发
                    # 新条件：量比<1.0（允许量平）+ 乖离±3%（接近均线即可）+ 今日下跌
                    shrink_ratio = self._cfg("tech_data", "shrinking_volume_ratio", default=1.0)
                    vol_ratio = data.get("volume_ratio", 1.0)
                    ma5_val = data.get("ma5", 0)
                    ma10_val = data.get("ma10", 0)
                    if vol_ratio < shrink_ratio:
                        pb5_tol = self._cfg("tech_data", "pullback_ma5_bias_tolerance", default=0.03)
                        if ma5_val > 0 and ma10_val > 0 and ma5_val > ma10_val:
                            bias5 = (closes[-1] - ma5_val) / ma5_val if ma5_val > 0 else 0
                            if -pb5_tol <= bias5 <= pb5_tol and closes[-1] < closes[-2]:
                                data["shrinking_pullback_ma5"] = True
                        pb10_tol = self._cfg("tech_data", "pullback_ma10_bias_tolerance", default=0.03)
                        if ma10_val > 0 and len(closes) >= 11:
                            ma10_prev = sum(closes[-11:-1]) / 10
                            if ma10_val > ma10_prev:
                                bias10 = (closes[-1] - ma10_val) / ma10_val if ma10_val > 0 else 0
                                if -pb10_tol <= bias10 <= pb10_tol and closes[-1] < closes[-2]:
                                    data["shrinking_pullback_ma10"] = True

                    # 对子底（D3 整改 2026-07-25：补"急跌跳水后"前置）
                    price_str = f"{closes[-1]:.2f}".replace(".", "")
                    is_pair = (price_str[-2:] == "99" or price_str[-2:] == "00" or
                               (len(price_str) >= 2 and price_str[-2] == price_str[-1]))
                    if is_pair:
                        drop_5d = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0
                        drop_1d = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
                        if drop_5d < -0.08 or drop_1d < -0.04:
                            data["pair_bottom"] = True

                    # 锤子线
                    hammer_ratio = self._cfg("tech_data", "hammer_lower_shadow_ratio", default=2)
                    if len(closes) >= 1 and len(opens) >= 1 and len(lows) >= 1:
                        body = abs(closes[-1] - opens[-1])
                        lower_shadow = min(closes[-1], opens[-1]) - lows[-1]
                        if body > 0 and lower_shadow > body * hammer_ratio:
                            data["has_hammer"] = True

                    # 跌停板被撬开（D4 整改 2026-07-25：补巨量封单+翘板资金前置）
                    ld_ratio = self._cfg("tech_data", "limit_down_ratio", default=0.9)
                    ld_touch = self._cfg("tech_data", "limit_down_touch_tolerance", default=1.002)
                    ld_open = self._cfg("tech_data", "limit_down_open_threshold", default=1.02)
                    if len(closes) >= 2 and len(lows) >= 1:
                        limit_down = closes[-2] * ld_ratio
                        if lows[-1] <= limit_down * ld_touch and closes[-1] > limit_down * ld_open:
                            vol_5d_avg = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 0
                            heavy_volume = volumes[-1] > vol_5d_avg * 2 if vol_5d_avg > 0 else False
                            today_open = opens[-1] if opens else closes[-1]
                            body = abs(closes[-1] - today_open)
                            lower_shadow = min(today_open, closes[-1]) - lows[-1]
                            sweep_fund = lower_shadow > body * 2 if body > 0 else lower_shadow > 0
                            if heavy_volume and sweep_fund:
                                data["daily_limit_opened"] = True

                    # 开盘强势
                    if len(closes) >= 2 and len(highs) >= 2:
                        data["opening_strong"] = closes[-1] > highs[-2]

                    if "sector_3d_return" not in data:
                        data["sector_3d_return"] = 0

                except (KeyError, ValueError, IndexError) as e:
                    logger.warning("Tech data parse error for %s: %s", stock_code, e)

        # 大盘数据
        if self._market_cache and "index_daily_drop" in self._market_cache:
            data["index_daily_drop"] = self._market_cache["index_daily_drop"]
        else:
            data["index_daily_drop"] = 0
        data["market_volume_yi"] = (self._market_cache or {}).get("market_volume_yi", 0)
        data["market_volume_20d_avg"] = (self._market_cache or {}).get("market_volume_20d_avg", 0)
        data["advance_decline_ratio"] = (self._market_cache or {}).get("advance_decline_ratio", 1.0)
        data["gem_sci_tech_drop"] = (self._market_cache or {}).get("gem_sci_tech_drop", 0)

        # 技术指标 + K 线形态
        tech_vote_min = self._cfg("tech_data", "min_klines_for_tech_vote", default=30)
        if data.get("kline") and len(data["kline"]) >= tech_vote_min:
            try:
                from ..data_layer.stock_data import calc_tech_indicators, detect_kline_patterns
                kline_data = data["kline"]
                tech = calc_tech_indicators(
                    kline_data,
                    market_mode,
                    volume_ratio=data.get("volume_ratio"),
                    volume_ratio_source=data.get("volume_ratio_source") or "K线5日均量",
                    realtime_quote=data,
                )
                if tech:
                    data["tech_signals"] = tech
                patterns = detect_kline_patterns(kline_data)
                if patterns:
                    data["kline_pattern"] = patterns
            except Exception as e:
                logger.debug("技术指标计算失败 %s: %s", stock_code, e)

        # RS line
        try:
            index_kline_data = self._market_cache.get("index_kline") if self._market_cache else None
            if index_kline_data and data.get("kline"):
                from ..data_layer.stock_data import calc_rs_line
                rs_result = calc_rs_line(data["kline"], index_kline_data)
                data["rs_line"] = rs_result
        except Exception:
            data["rs_line"] = {"rs_uptrend": False, "rs_ma_trend": "计算失败"}

        # 周线MACD过滤
        weekly_macd_up = self._compute_weekly_macd_up(stock_code, data.get("kline", []))
        data["weekly_macd_up"] = weekly_macd_up

        # 机构持仓打分（4 数据源投票，API 失败默认中性，session 缓存 1 小时）
        try:
            from .institutional_scorer import score_institutional_holding
            # 【P0-6】大宗交易第五资金源：生产侧注入真实抓取器；
            # 无龙虎榜无两融的标的（北交所）由该源补位。
            try:
                from ..data_layer.block_trade import fetch_block_trade_stats
                from .institutional_scorer import set_block_trade_fetcher
                set_block_trade_fetcher(fetch_block_trade_stats)
            except Exception:
                pass
            inst_score = score_institutional_holding(
                stock_code,
                turnover_available=bool(data.get("turnover_rate")),
            )
            data["institutional_holding"] = inst_score
        except Exception as e:
            logger.debug("机构持仓打分失败 %s: %s", stock_code, e)
            data["institutional_holding"] = {
                "vote_score": 0, "vote_label": "资金中性",
                "votes": {}, "bullish_count": 0, "bearish_count": 0,
                "neutral_count": 4, "stale": False,
            }

        # 【二】基本面快照（Phase2-A）：业绩快报/预告/扣非 → 业绩雷否决、
        # 盈利质量降级、财报窗口提示。数据源全部失败时保持缺省（闸门放行，
        # 不产生假基本面结论）。报告期级数据，推送时必须带口径展示。
        try:
            from .fundamental_gate import fetch_fundamental_snapshot
            fund_snapshot = fetch_fundamental_snapshot(stock_code, data.get("stock_name", "") or "")
            if fund_snapshot:
                data["fundamental"] = fund_snapshot
        except Exception as e:
            logger.debug("基本面快照获取失败 %s: %s", stock_code, e)

        # 【Phase3】估值透镜：PE/PB/净利绝对值/合同负债/存货/研报共识。
        # 观察卡与信号出厂共用；此处做中性口径评估（供展示），
        # signal_plan.build_execution_plan 按 entry_type 重新评估——
        # 追高型策略（确认追强/价量突破）对 model_loss/估值泡沫更严格。
        # 数据源全部失败时保持缺省（透镜放行，不产生假估值结论）。
        try:
            from .valuation_lens import assemble_valuation_lens, attach_analyst_to_institutional
            flow_vote = None
            if isinstance(data.get("institutional_holding"), dict):
                flow_vote = data["institutional_holding"].get("vote_score")
            lens = assemble_valuation_lens(
                stock_code,
                data.get("stock_name", "") or "",
                fundamental=data.get("fundamental"),
                flow_vote=flow_vote,
                config=self._tc,
            )
            if lens:
                data["valuation_lens"] = lens
                # 分析师共识旁路注入机构投票结果（渲染层双标签呈现，不混票）
                if isinstance(data.get("institutional_holding"), dict):
                    attach_analyst_to_institutional(data["institutional_holding"], lens)
        except Exception as e:
            logger.debug("估值透镜获取失败 %s: %s", stock_code, e)

        return data

    def _compute_weekly_macd_up(self, stock_code: str, daily_kline: List[Dict]) -> bool:
        """
        计算周线 MACD 是否向上（DIF > DEA）

        实盘模式和回测模式都从日 K 线聚合为周 K 线再算。
        修复 BUG-A1: DEA 恒等于 DIF（原算法用单日 DIF 做无效递归）
        修复 BUG-B5: EMA12 平滑系数公式错误
        修复 BUG-B6/B18: 实盘模式也聚合周线（不再仅限回测模式）
        """
        weekly_kline = self._tech_cache_weekly.get(stock_code)
        if weekly_kline is None and daily_kline:
            # 实盘+回测都从日 K 聚合周 K
            weekly_kline = self._aggregate_weekly_from_daily(daily_kline)
            if weekly_kline:
                self._tech_cache_weekly[stock_code] = weekly_kline

        if not weekly_kline:
            return False

        min_weeks = self._cfg("tech_data", "weekly_macd_min_weeks", default=26)
        if len(weekly_kline) < min_weeks:
            return False

        try:
            w_closes = [float(k.get("收盘", k.get("close", 0))) for k in weekly_kline]
            if len(w_closes) < min_weeks:
                return False

            ema12_period = self._cfg("tech_data", "ema12_period", default=12)
            ema26_period = self._cfg("tech_data", "ema26_period", default=26)
            dea_period = 9  # DEA 是 DIF 的 EMA9

            # 标准 EMA 计算：用前 N 个值的 SMA 作为种子，然后逐日更新
            # EMA_N = price * 2/(N+1) + EMA_N_prev * (N-1)/(N+1)
            def calc_ema(values: List[float], period: int) -> List[float]:
                if len(values) < period:
                    return []
                # 种子 = 前 period 个值的 SMA
                seed = sum(values[:period]) / period
                emas = [0.0] * (period - 1) + [seed]
                alpha = 2.0 / (period + 1)
                for i in range(period, len(values)):
                    emas.append(values[i] * alpha + emas[-1] * (1 - alpha))
                return emas

            ema12_list = calc_ema(w_closes, ema12_period)
            ema26_list = calc_ema(w_closes, ema26_period)
            if not ema12_list or not ema26_list:
                return False

            # DIF 序列 = EMA12 - EMA26（对齐到相同长度）
            min_len = min(len(ema12_list), len(ema26_list))
            dif_list = []
            for i in range(1, min_len + 1):
                dif_list.append(ema12_list[-i] - ema26_list[-i])
            dif_list.reverse()

            # DEA = DIF 的 EMA9
            dea_list = calc_ema(dif_list, dea_period)
            if not dea_list:
                return False

            # 最新 DIF > DEA → 周线 MACD 向上
            return dif_list[-1] > dea_list[-1]
        except Exception:
            return False

    @staticmethod
    def _aggregate_weekly_from_daily(daily_kline: List[Dict]) -> List[Dict]:
        """从日 K 线聚合为周 K 线（按自然周聚合，周一为周首）"""
        if not daily_kline:
            return []
        from datetime import timedelta  # 修复 BUG-T2: datetime 已在模块顶层导入，此处重复导入遮蔽同名顶层符号
        weekly = []
        current_week = None
        week_rows = []
        for row in daily_kline:
            try:
                # 修复 BUG-B7: 兼容中文键 "日期"
                d = row.get("date", row.get("日期", ""))
                if not d:
                    continue
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
                # ISO 周一为周首
                week_start = dt - timedelta(days=dt.weekday())
                if current_week is None:
                    current_week = week_start
                    week_rows = [row]
                elif week_start == current_week:
                    week_rows.append(row)
                else:
                    if week_rows:
                        weekly.append(TimingEngine._aggregate_week_rows(week_rows))
                    current_week = week_start
                    week_rows = [row]
            except Exception:
                continue
        if week_rows:
            weekly.append(TimingEngine._aggregate_week_rows(week_rows))
        return weekly

    @staticmethod
    def _aggregate_week_rows(rows: List[Dict]) -> Dict:
        """聚合一周的日 K 为单根周 K"""
        if not rows:
            return {}
        opens = [float(r.get("开盘", r.get("open", 0)) or 0) for r in rows]
        closes = [float(r.get("收盘", r.get("close", 0)) or 0) for r in rows]
        highs = [float(r.get("最高", r.get("high", 0)) or 0) for r in rows]
        lows = [float(r.get("最低", r.get("low", 0)) or 0) for r in rows]
        vols = [float(r.get("成交量", r.get("volume", 0)) or 0) for r in rows]
        return {
            "date": rows[-1].get("date", ""),
            "open": opens[0] if opens else 0,
            "close": closes[-1] if closes else 0,
            "high": max(highs) if highs else 0,
            "low": min(lows) if lows else 0,
            "volume": sum(vols),
        }

    def _calculate_target_range(self, tech_data: Dict, entry_type: str) -> List[float]:
        """计算止盈目标区间"""
        tr = self._tc.get("target_range", {})
        current = tech_data.get("current_price", 0)
        resistance = tech_data.get("resistance", 0) or tech_data.get("recent_high", 0)

        if entry_type == "恐慌抄底":
            return [round(current * tr.get("panic_bottom_low", 1.08), 2),
                    round(current * tr.get("panic_bottom_high", 1.18), 2)]
        elif entry_type == "套利低吸":
            # C5 市况自适应数值纪律（2026-07-25 整改）
            mode = getattr(self, '_current_market_mode', 'defend')
            if mode == 'attack':
                return [round(current * 0.98, 2), round(current * 1.08, 2)]  # 强势日 +8%
            elif mode == 'retreat':
                return [round(current * 0.98, 2), round(current * 1.02, 2)]  # 撤退不套利
            else:
                return [round(current * 0.98, 2), round(current * 1.03, 2)]  # 震荡市 -2/+3
        elif entry_type == "确认追强":
            return [round(current * tr.get("momentum_chase_low", 1.05), 2),
                    round(current * tr.get("momentum_chase_high", 1.12), 2)]
        elif entry_type == "价量突破":
            return [round(current * tr.get("volume_breakout_low", 1.08), 2),
                    round(current * tr.get("volume_breakout_high", 1.15), 2)]
        else:
            if resistance > current:
                return [round(resistance * tr.get("default_with_resistance_low", 0.97), 2),
                        round(resistance * tr.get("default_with_resistance_high", 1.02), 2)]
            return [round(current * tr.get("default_no_resistance_low", 1.05), 2),
                    round(current * tr.get("default_no_resistance_high", 1.08), 2)]


# 单例
_instance: Optional[TimingEngine] = None
_instance_backtest: Optional[TimingEngine] = None


def get_timing_engine() -> TimingEngine:
    """获取实盘单例（backtest_mode=False）"""
    global _instance
    if _instance is None:
        _instance = TimingEngine(backtest_mode=False)
    return _instance


def get_backtest_timing_engine(params_override: Optional[dict] = None) -> TimingEngine:
    """
    获取回测专用实例（backtest_mode=True）

    每次调用都返回新实例（回测需要独立状态），并支持 params_override 用于网格搜索。
    """
    return TimingEngine(backtest_mode=True, params_override=params_override)


# ============================================================
# P0 新增：并发安全工厂函数
# ============================================================

# 线程局部存储，每个线程持有独立的 TimingEngine 实例
# 用于 Walk-Forward 并行回测场景，避免单例状态污染


def create_timing_engine(backtest_mode: bool = False,
                         params_override: Optional[dict] = None) -> "TimingEngine":
    """
    创建独立的 TimingEngine 实例（非单例）

    用于：
    1. Walk-Forward 并行回测（每个 fold 独立实例）
    2. 网格搜索并行执行（每个参数组合独立实例）
    3. unified_engine 并行调用（每个线程独立实例）

    与 get_timing_engine() 的区别：
    - get_timing_engine()：返回全局单例，实盘路径用
    - create_timing_engine()：返回新实例，并发场景用
    """
    return TimingEngine(backtest_mode=backtest_mode, params_override=params_override)


def get_thread_local_timing_engine(backtest_mode: bool = False,
                                   params_override: Optional[dict] = None) -> "TimingEngine":
    """
    获取线程局部的 TimingEngine 实例

    每个线程首次调用时创建新实例并缓存到 thread_local，后续同线程调用复用。
    适合 ThreadPoolExecutor 并行回测场景。

    注意：params_override 仅在线程首次调用时生效，后续调用忽略。
    """
    if not hasattr(_thread_local, "timing_engine"):
        _thread_local.timing_engine = TimingEngine(
            backtest_mode=backtest_mode,
            params_override=params_override,
        )
    return _thread_local.timing_engine


def reset_thread_local_timing_engine() -> None:
    """重置当前线程的 TimingEngine 实例（下下次调用重新创建）"""
    if hasattr(_thread_local, "timing_engine"):
        del _thread_local.timing_engine
