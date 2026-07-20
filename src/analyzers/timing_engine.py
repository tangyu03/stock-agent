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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime

from ..config_models import load_config
from ..data_layer.akshare_adapter import get_akshare_adapter
from ..data_layer.skill_wrapper import get_skill_wrapper
from ..analyzers.stock_filter import get_stock_filter, FilterResult

logger = logging.getLogger(__name__)


@dataclass
class EntrySignal:
    """入场信号"""
    stock_code: str
    stock_name: str
    entry_type: str               # 恐慌抄底 / 套利低吸 / 确认追强
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
    concepts: str = ""            # 概念板块（逗号分隔）
    concept_status: str = ""      # 概念板块状态（主线/轮动/退潮）
    trigger_reason: str = ""       # 触发原因描述
    strategy_summary: str = ""     # 策略逻辑透传
    confidence: str = "中"         # 信号置信度
    kline_patterns: List[Dict] = field(default_factory=list)
    tech_data: Dict = field(default_factory=dict)


@dataclass
class ExitSignal:
    """出场信号"""
    stock_code: str
    stock_name: str
    exit_type: str                 # 破位止损 / 破位预警 / 冲高止盈 / MA5压制 / 技术走弱 / 板块退潮
    trigger_price: float
    stop_loss_price: float
    reason: str
    urgency: str = "重要"
    mode_constrained: bool = False
    sector_status: str = ""
    sector_name: str = ""
    sw_level3: str = ""
    concepts: str = ""
    tech_data: Dict = field(default_factory=dict)

@dataclass
class StopLossCalc:
    """止损价计算结果"""
    stock_code: str
    current_price: float
    support_candidates: List[Dict[str, float]]
    chosen_support: float
    stop_loss_price: float
    resistance: float


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
        "min_trigger_conditions": 1,
    },
    "momentum_chase": {
        "ma20_trend_min_klines": 21,
        "ma20_window": 20,
        "breakout_price_ratio": 0.99,
        "volume_confirm_ratio": 1.2,
    },
    "volume_breakout": {
        "require_rs_above_ma": True,
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
        "fallback_support_ratio": 0.92,  # 修复 BUG-E1: 从 0.97 改为 0.92（支撑位过远时用）
        "max_support_distance": 0.12,    # 修复 BUG-E1: 支撑位距离超过 12% 时回退
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
        "volume_ratio_avg_window": 21,
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

        # 兼容：risk.yaml 的 stop_loss_multiplier（已迁移到 timing.yaml stop_loss.multiplier）
        self._risk_config = {}
        try:
            self._risk_config = load_config("risk.yaml").get("risk", {})
        except Exception:
            pass

        self._akshare = get_akshare_adapter()
        self._skill = get_skill_wrapper()
        self._stock_filter = get_stock_filter()
        self._tech_cache: Dict[str, Dict] = {}
        self._tech_cache_weekly: Dict[str, List[Dict]] = {}  # 修复 bug: 原代码从未初始化
        self._tech_data_full: Dict[str, Dict] = {}  # 完整 tech_data 缓存（含技术指标+机构打分），供观察列表复用
        self._market_cache: Optional[Dict] = None
        self._cache_lock = threading.Lock()

        # 回测模式上下文
        self._backtest_mode = backtest_mode
        self._backtest_kline: Dict[str, List[Dict]] = {}
        self._backtest_index_kline: List[Dict] = []
        self._backtest_date: str = ""

    # ============================================================
    # 配置访问辅助方法
    # ============================================================

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
            self._market_cache = None

    def reset_caches(self):
        """重置本轮缓存（每次 run_unified_analysis 前调用）"""
        with self._cache_lock:
            self._tech_cache.clear()
            self._tech_cache_weekly.clear()
            self._tech_data_full.clear()
            self._market_cache = None

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
        except Exception:
            pass
        # 全市场成交额
        try:
            vol_result = self._akshare.get_market_volume()
            if vol_result.success and vol_result.data:
                data["market_volume_yi"] = vol_result.data.get("total_volume_yi", 0)
        except Exception:
            pass
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
        except Exception:
            pass
        # 涨跌比
        try:
            ad_result = self._akshare.get_advance_decline()
            if ad_result.success and ad_result.data:
                data["advance_decline_ratio"] = ad_result.data.get("advance_decline_ratio", 1.0)
        except Exception:
            pass
        # 缓存指数K线（供RS line计算）
        try:
            if index_result and index_result.success and index_result.data:
                data["index_kline"] = index_result.data
        except Exception:
            pass
        # 双创跌幅
        try:
            from ..analyzers.gem_sci_tech_scorer import get_gem_sci_tech_analysis
            gst = get_gem_sci_tech_analysis()
            gem = gst.get("gem") or {}
            star = gst.get("star") or {}
            gem_drop = abs(gem.get("change_pct", 0)) if gem.get("change_pct", 0) < 0 else 0
            star_drop = abs(star.get("change_pct", 0)) if star.get("change_pct", 0) < 0 else 0
            data["gem_sci_tech_drop"] = max(gem_drop, star_drop)
        except Exception:
            pass
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
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for code in new_codes:
                futures[pool.submit(self._akshare.get_stock_hist, code)] = code
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

    _ENTRY_PRIORITY = ["恐慌抄底", "套利低吸", "确认追强", "价量突破"]

    def _merge_entry_signals(self, signals: List[EntrySignal], tech_data: Dict, market_mode: str, sector_status: str = "") -> List[EntrySignal]:
        """合并多条入场信号为一条综合信号，附带策略推导链。"""
        if not signals:
            return []

        triggered_types = list(dict.fromkeys(s.entry_type for s in signals))

        if len(signals) == 1:
            sig = signals[0]
            derivation = self._build_derivation(
                tech_data, market_mode,
                triggered_types=triggered_types,
                selected_type=sig.entry_type,
                confidence=getattr(sig, "confidence", "中"),
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
            confidence=getattr(best, "confidence", "中"),
            signal_direction="entry",
            sector_status=sector_status,
        )
        best.trigger_reason += "\n  推导: " + derivation

        logger.info("入场信号合并: %s %d->1条（主类型=%s）", best.stock_code, len(signals), best_type)
        return [best]


    def check_entry_signals(
        self,
        stock_code: str,
        stock_name: str = "",
        market_mode: str = "defend",
        sector_status: str = "rotational",
        filter_result: Optional[FilterResult] = None,
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

        # 获取技术数据 + 止损价
        tech_data = self._fetch_tech_data(stock_code, market_mode)
        stop_loss_calc = self.calculate_stop_loss(stock_code, tech_data)

        # 独立检查四种进场策略
        raw_signals = []
        for check_fn, etype in [
            (self._check_panic_bottom, "恐慌抄底"),
            (self._check_arbitrage_entry, "套利低吸"),
            (self._check_momentum_chase, "确认追强"),
            (self._check_volume_breakout, "价量突破"),
        ]:
            sig = check_fn(stock_code, stock_name, tech_data, stop_loss_calc, market_mode, sector_status)
            if sig:
                raw_signals.append(sig)

        if not raw_signals:
            return []

        # 合并为一条综合信号
        merged = self._merge_entry_signals(raw_signals, tech_data, market_mode, sector_status)
        for sig in merged:
            sig.tech_data = tech_data
        return merged

    def _check_panic_bottom(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        恐慌抄底 - 大盘恐慌 + 个股超卖
        """
        panic_market = []

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

        # 新增：成交量 250 日分位（来自市场环境增强模块）
        # 放量宣泄（分位 > 70%）= 恐慌集中释放，可能接近底部
        # 缩量阴跌（分位 < 30%）= 持续阴跌，不宜抄底
        try:
            from .market_env import get_market_environment
            mkt_env = get_market_environment()
            vol_pct = mkt_env.get("volume_percentile", 50)
            if vol_pct >= 70:
                panic_market.append(f"放量宣泄(分位{vol_pct:.0f}%)")
            elif vol_pct < 30:
                # 缩量阴跌不抄底 — 从 panic_market 中移除已有的放量信号
                panic_market = [p for p in panic_market if "放量" not in p and "巨量" not in p]
                panic_market.append(f"缩量阴跌(分位{vol_pct:.0f}%)不宜抄底")
        except Exception:
            pass

        if not panic_market:
            return None

        # 过滤：缩量阴跌不宜抄底
        if any("不宜抄底" in p for p in panic_market):
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

        if sector == "retreating":
            return None

        # 市场恐慌≥1 + 个股超卖≥1 即可触发
        # 关键不是数量，而是超卖定义的准确性 — 每个条件都是真正的超卖信号
        all_conds = panic_market + stock_oversold
        trigger_reason = ";".join(all_conds)
        hc_market = self._cfg("panic_bottom", "high_confidence_market_conds", default=1)
        hc_stock = self._cfg("panic_bottom", "high_confidence_stock_conds", default=2)
        confidence = "高" if len(panic_market) >= hc_market and len(stock_oversold) >= hc_stock else "中"

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
        if sector == "retreating":
            return None

        # 周线MACD过滤
        if not tech_data.get("weekly_macd_up"):
            return None

        conditions = []
        shrinking_ratio = self._cfg("arbitrage", "shrinking_volume_ratio", default=1.0)
        is_shrinking = tech_data.get("volume_ratio", 1.0) < shrinking_ratio

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

    def _check_momentum_chase(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        确认追强（海龟突破）
        """
        if mode not in ("attack",):
            return None
        if sector == "retreating":
            return None

        current = tech_data.get("current_price", 0)
        ma20 = tech_data.get("ma20", 0)
        recent_high = tech_data.get("recent_high", 0)
        vol_ratio = tech_data.get("volume_ratio", 1.0)
        kline = tech_data.get("kline", [])

        if not all([current, ma20, recent_high]):
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

        # Donchian 20日突破
        breakout_ratio = self._cfg("momentum_chase", "breakout_price_ratio", default=0.99)
        if current < recent_high * breakout_ratio:
            return None

        # 放量确认
        vol_confirm = self._cfg("momentum_chase", "volume_confirm_ratio", default=1.2)
        if vol_ratio < vol_confirm:
            return None

        conditions = [
            f"海龟突破(MA20趋势向上, 突破{recent_high:.2f}, 放量{vol_ratio:.1f}倍)",
        ]
        return EntrySignal(
            stock_code=code,
            stock_name=name,
            entry_type="确认追强",
            strategy_summary="确认追强 — 突破关键位 + 放量确认 + RS线强势",
            entry_trigger_price=current,
            stop_loss=stop_loss.stop_loss_price,
            target_type="突破持有",
            target_range=self._calculate_target_range(tech_data, "确认追强"),
            position_level="spread",
            applicable_modes=["attack"],
            sector_status=sector,
            trigger_reason="；".join(conditions),
            confidence="高",
        )

    def _check_volume_breakout(self, code, name, tech_data, stop_loss, mode, sector) -> Optional[EntrySignal]:
        """
        价量突破型（右侧个股级突破，不依赖板块启动）
        """
        if mode not in ("attack", "defend"):
            return None
        if sector == "retreating":
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
        ma25_prev = tech_data.get("ma25_prev", 0)
        prev_close = tech_data.get("prev_close", 0)
        today_volume = tech_data.get("today_volume", 0)
        volume_ma60 = tech_data.get("volume_ma60", 0)

        if not all([current, ma25, prev_close, today_volume, volume_ma60]):
            return None

        # 站上 MA25（不要求"首次突破"，已站上的也算趋势延续）
        # 原条件 prev_close <= ma25_prev 太严格，只在首次突破当天触发
        # 新条件：当前价站上 MA25 即可（趋势中或突破都算）
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
        volume_breakout = today_volume > volume_ma60
        if not volume_breakout and not is_limit_up_today:
            return None

        vol_ratio = today_volume / volume_ma60 if volume_ma60 > 0 else 0
        conditions = [
            f"站上MA25({ma25:.2f})",
            f"量能突破60日均量({vol_ratio:.1f}倍)" if volume_breakout else f"涨停豁免量能(涨幅{(current-prev_close)/prev_close*100:.1f}%)",
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

    # ============ 出场信号 ============

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
        """
        signals = []

        tech_data = self._fetch_tech_data(stock_code, market_mode)
        # 缓存完整 tech_data（含技术指标+机构打分），供 engine.py 观察列表复用
        self._tech_data_full[stock_code] = tech_data
        stop_loss_calc = self.calculate_stop_loss(stock_code, tech_data)

        current_price = tech_data.get("current_price", 0)

        # 技术指标投票 + K 线形态
        tech_vote = tech_data.get("tech_signals", {}).get("vote", "中性")
        tech_score = tech_data.get("tech_signals", {}).get("vote_score", 0)
        kline_patterns = tech_data.get("kline_pattern", [])

        _bearish_votes = {"强烈看空", "偏空", "温和偏空"}
        is_bearish = tech_vote in _bearish_votes
        is_not_bearish = tech_vote not in _bearish_votes and tech_vote != "中性"

        # 1. 破位止损
        stop_triggered = current_price <= stop_loss_calc.stop_loss_price
        if stop_triggered:
            kline_raw = tech_data.get('kline', [])
            last_k = kline_raw[-1] if kline_raw else {}; last_close = float(last_k.get("收盘", last_k.get("close", current_price)))
            vol_ratio = tech_data.get('volume_ratio', 1.0)

            close_below = last_close <= stop_loss_calc.stop_loss_price
            vol_confirm_thresh = self._cfg("exit", "breakdown", "volume_confirm_ratio", default=0.8)
            vol_confirm = vol_ratio > vol_confirm_thresh
            tech_confirm = is_bearish or tech_vote == '中性'

            is_valid = close_below and vol_confirm and tech_confirm

            if is_valid:
                reason = f'跌破{stop_loss_calc.chosen_support:.2f}(止损{stop_loss_calc.stop_loss_price:.2f})，收盘确认'
                heavy_vol_thresh = self._cfg("exit", "breakdown", "heavy_volume_ratio", default=1.3)
                if vol_ratio > heavy_vol_thresh:
                    reason += '，放量破位(确认有效)'
                if is_bearish:
                    reason += f'；{tech_vote}(score={tech_score:+.1f})共振'
                bpats = [p.get('pattern','') for p in kline_patterns if '看跌' in p.get('signal','') or '压力' in p.get('signal','')]
                if bpats:
                    reason += '; K线:' + ','.join(bpats)
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name, exit_type='破位止损',
                    trigger_price=current_price, stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=reason, urgency='紧急', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))
            else:
                skip = []
                if not close_below:
                    skip.append(f'收盘{last_close:.2f}拉回支撑上方')
                if not vol_confirm:
                    skip.append(f'缩量(量比{vol_ratio:.2f})卖压不足')
                if not tech_confirm:
                    skip.append(f'{tech_vote}({tech_score:+.1f})偏多')
                warn_reason = '跌破' + f'{stop_loss_calc.chosen_support:.2f}(止损{stop_loss_calc.stop_loss_price:.2f})' + '但' + '; '.join(skip)
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name, exit_type='破位预警',
                    trigger_price=current_price, stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=warn_reason, urgency='观察', mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))
                logger.info(
                    '破位预警 %s: 触发%.2f<止损%.2f，%s',
                    stock_code, current_price, stop_loss_calc.stop_loss_price,
                    '; '.join(skip))

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
                vol_trend = vols_hist[-1] < sum(vols_hist[:vp_avg_window]) / vp_avg_window * vol_shrink
                if vol_trend and current_price >= max(prices_hist[:-1]):
                    exhaustion.append(('strong', '量价背离(价新高量缩)'))

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

        # ── 均线乖离（冲高止盈：价格偏离均线过远）──
        ma5 = tech_data.get('ma5')
        if ma5 and ma5 > 0:
            bias = (current_price - ma5) / ma5
            ma5_overheat = self._cfg("exit", "exhaustion", "ma5_bias_overheat", default=0.08)
            if bias > ma5_overheat:
                exhaustion.append(('medium', f'MA5乖离{bias*100:.1f}%(过热)'))

        # ── MA5 压制（核心出场信号：沿MA5趋势被破坏）──
        # 与进场逻辑对称：进场要求"站上MA5"(price>ma5)，出场就是"跌破MA5"
        # 这是"沿MA5做"交易体系的核心：站上MA5买入，跌破MA5卖出
        #
        # 触发条件（全部满足）：
        # 1. 多头排列（ma5 > ma10 > ma20）— 确保在上升趋势中，震荡市不触发
        # 2. MA5 仍上升（ma5_prev > ma5_prev2）— 趋势还没转头
        # 3. 当前价跌破 MA5 超过 1% — 不是毛刺，是有效跌破
        ma5_pressure_signal = None
        ma10 = tech_data.get('ma10')
        ma20 = tech_data.get('ma20')
        ma5_prev = tech_data.get('ma5_prev')
        ma5_prev2 = tech_data.get('ma5_prev2')
        if ma5 and ma5 > 0 and ma10 and ma20 and current_price > 0 and ma5_prev and ma5_prev2:
            is_bullish_alignment = ma5 > ma10 > ma20  # 多头排列
            ma5_rising = ma5_prev > ma5_prev2          # MA5 仍上升
            # 有效跌破：价格低于 MA5 超过 1%（过滤盘中毛刺）
            ma5_break_pct = (ma5 - current_price) / ma5
            price_below_ma5 = ma5_break_pct > 0.01     # 跌破 MA5 超过 1%
            if is_bullish_alignment and ma5_rising and price_below_ma5:
                ma5_pressure_signal = f'MA5压制(价{current_price:.2f}<MA5:{ma5:.2f},跌{ma5_break_pct*100:.1f}%,多头排列)'

        # ── K线看跌形态 ──
        for p in kline_patterns:
            sig = p.get('signal', '')
            if '看跌' in sig:
                pat = p.get('pattern', '')
                if '吞没' in pat or '乌云' in pat:
                    exhaustion.append(('medium', f'K线:{pat}'))

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

        # 2. MA5压制（核心出场信号：沿MA5趋势被破坏，与进场"站上MA5"对称）
        # 独立推送，不需要其他信号叠加 — MA5压制本身就是完整的交易信号
        if ma5_pressure_signal:
            signals.append(ExitSignal(
                stock_code=stock_code, stock_name=stock_name,
                exit_type='MA5压制', trigger_price=current_price,
                stop_loss_price=stop_loss_calc.stop_loss_price,
                reason=ma5_pressure_signal, urgency='重要', mode_constrained=False,
                sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
            ))

        # 3. 技术走弱（投票强烈看空/偏空/布林下轨）
        if weakness:
            strong = sum(1 for s in weakness if s[0] == 'strong')
            medium = sum(1 for s in weakness if s[0] == 'medium')
            # 技术走弱：strong≥1（投票强烈看空）或 medium≥3（多个走弱信号叠加）才推送
            if strong >= 1 or medium >= 3:
                labels = [f'[{lvl}]{lbl}' for lvl, lbl in weakness]
                reason = '；'.join(labels)
                urgency = '重要' if strong >= 1 else '观察'
                signals.append(ExitSignal(
                    stock_code=stock_code, stock_name=stock_name,
                    exit_type='技术走弱', trigger_price=current_price,
                    stop_loss_price=stop_loss_calc.stop_loss_price,
                    reason=reason, urgency=urgency, mode_constrained=False,
                    sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
                ))

        # 3. 板块退潮 — 已移除（用户反馈：影响太大，板块退潮不应直接触发卖出）
        # 板块退潮信息仍会在推导链和持仓健康度里展示，但不再作为独立卖出信号
        # if sector_status == 'retreating':
        #     severity = '紧急' if market_mode == 'retreat' else '重要'
        #     reason = '板块退潮'
        #     if market_mode == 'retreat':
        #         reason += '+市场撤退 — 建议清仓'
        #     else:
        #         reason += ' — 注意风险，考虑减仓或收紧止损'
        #     signals.append(ExitSignal(
        #         stock_code=stock_code, stock_name=stock_name,
        #         exit_type='板块退潮', trigger_price=current_price,
        #         stop_loss_price=stop_loss_calc.stop_loss_price,
        #         reason=reason, urgency=severity, mode_constrained=False,
        #         sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
        #     ))

        # 合并同类 + 追加推导链
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
            sig.reason += "\n  推导: " + derivation

        return list(merged.values())

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

        if not candidates:
            chosen_support = current_price * fallback_ratio
        else:
            below = [c for c in candidates if c["value"] < current_price]
            if below:
                # 选最高的支撑位（最接近当前价）
                chosen_support = max(below, key=lambda c: c["value"])["value"]
                # 修复 BUG-E1: 如果选中的支撑位距离当前价过远，回退到固定百分比
                if current_price > 0 and (current_price - chosen_support) / current_price > max_support_distance:
                    chosen_support = current_price * fallback_ratio
            else:
                # 所有 MA 都在当前价上方 → 刚破位，用固定百分比
                chosen_support = current_price * fallback_ratio

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
        stop_loss_multiplier = self._cfg("stop_loss", "multiplier", default=0.97)
        stop_loss_price = chosen_support * stop_loss_multiplier - buffer

        prev_high = tech_data.get("prev_high")
        resistance = prev_high or 0
        recent_high = tech_data.get("recent_high", 0)
        if recent_high > resistance:
            resistance = recent_high

        return StopLossCalc(
            stock_code=stock_code,
            current_price=current_price,
            support_candidates=candidates,
            chosen_support=chosen_support,
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
                strategy_note = "可用:恐慌抄底/套利低吸 | 禁用:确认追强"
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
                contradictions.append(f"MA空头⚠️" if ma_dir < 0 else "MA多头(与投票反向)")

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
            inst_label = inst.get("vote_label", "机构中性")
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
                f"②.5机构: {inst_label}({inst_score:+d}票,看多{inst_bull}/看空{inst_bear}) "
                f"[{vote_summary}]{stale_mark}"
            )

            # 机构与技术面共振/矛盾判断
            if inst_score >= 2 and vote_dir > 0:
                lines.append(f"   ↳ 机构与技术共振看多✅")
            elif inst_score <= -2 and vote_dir < 0:
                lines.append(f"   ↳ 机构与技术共振看空⚠️")
            elif inst_score >= 2 and vote_dir < 0:
                lines.append(f"   ↳ ⚠️机构看多但技术看空(分歧)")
            elif inst_score <= -2 and vote_dir > 0:
                lines.append(f"   ↳ ⚠️机构看空但技术看多(分歧)")

        # ③ 信号触发
        if triggered_types:
            lines.append(f"③触发: {'/'.join(triggered_types)}")
        else:
            lines.append("③触发: 无明确信号")

        # ④ 决策
        if selected_type:
            lines.append(f"④决策: 选取{selected_type} | 置信度:{confidence}")

        return "\n  ".join(lines)

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
                return {"price": q.get("current_price", 0), "change_pct": q.get("change_pct", 0)}
        except Exception:
            pass
        return None

    def _fetch_tech_data(self, stock_code: str, market_mode: str = "defend") -> Dict[str, Any]:
        """获取技术分析数据（含多级均线 + 盘中实时信号）"""
        data = {}

        # 从预取缓存获取实时价
        realtime = self._get_realtime_price(stock_code)
        if realtime:
            data["current_price"] = realtime.get("price", 0)
            data["change_pct"] = realtime.get("change_pct", 0)

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
                hist_result = self._akshare.get_stock_hist(stock_code)
                if hist_result.success and hist_result.data:
                    kline = hist_result.data

        if kline:
            data["kline"] = kline
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
                        data["volume_ma60"] = sum(volumes[-vol_ma60_window:]) / vol_ma60_window
                        data["today_volume"] = volumes[-1]

                    extreme_window = self._cfg("tech_data", "recent_extreme_window", default=20)
                    data["prev_low"] = min(lows[-extreme_window:]) if len(lows) >= extreme_window else (lows[-1] if lows else None)
                    data["prev_high"] = highs[-1] if len(highs) >= 1 else None
                    data["recent_high"] = max(highs[-extreme_window:]) if len(highs) >= extreme_window else max(highs)

                    vol_ratio_window = self._cfg("tech_data", "volume_ratio_avg_window", default=21)
                    if len(volumes) >= vol_ratio_window:
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

                    # 对子底（修复 BUG-B4: 去掉小数点后再判断对子）
                    # 对子底定义：价格尾数两位相同（如 12.22, 33.33, 5.55）或 99/00 结尾
                    price_str = f"{closes[-1]:.2f}".replace(".", "")
                    data["pair_bottom"] = (
                        price_str[-2:] == "99" or price_str[-2:] == "00" or
                        (len(price_str) >= 2 and price_str[-2] == price_str[-1])
                    )

                    # 锤子线
                    hammer_ratio = self._cfg("tech_data", "hammer_lower_shadow_ratio", default=2)
                    if len(closes) >= 1 and len(opens) >= 1 and len(lows) >= 1:
                        body = abs(closes[-1] - opens[-1])
                        lower_shadow = min(closes[-1], opens[-1]) - lows[-1]
                        if body > 0 and lower_shadow > body * hammer_ratio:
                            data["has_hammer"] = True

                    # 跌停板被撬开
                    ld_ratio = self._cfg("tech_data", "limit_down_ratio", default=0.9)
                    ld_touch = self._cfg("tech_data", "limit_down_touch_tolerance", default=1.002)
                    ld_open = self._cfg("tech_data", "limit_down_open_threshold", default=1.02)
                    if len(closes) >= 2 and len(lows) >= 1:
                        limit_down = closes[-2] * ld_ratio
                        if lows[-1] <= limit_down * ld_touch and closes[-1] > limit_down * ld_open:
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
                tech = calc_tech_indicators(kline_data, market_mode)
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
            inst_score = score_institutional_holding(stock_code)
            data["institutional_holding"] = inst_score
        except Exception as e:
            logger.debug("机构持仓打分失败 %s: %s", stock_code, e)
            data["institutional_holding"] = {
                "vote_score": 0, "vote_label": "机构中性",
                "votes": {}, "bullish_count": 0, "bearish_count": 0,
                "neutral_count": 4, "stale": False,
            }

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
        from datetime import datetime, timedelta
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
            if resistance > current:
                return [round(resistance * tr.get("arbitrage_with_resistance_low", 0.97), 2),
                        round(resistance * tr.get("arbitrage_with_resistance_high", 1.02), 2)]
            return [round(current * tr.get("arbitrage_no_resistance_low", 1.05), 2),
                    round(current * tr.get("arbitrage_no_resistance_high", 1.08), 2)]
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
import threading as _threading
_thread_local = _threading.local()


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
