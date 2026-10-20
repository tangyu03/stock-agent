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


from .timing_models import EntrySignal, ExitSignal, StopLossCalc  # noqa: F401
from .timing_data_fetch import TimingDataFetchMixin
from .timing_entry_checkers import TimingEntryMixin
from .timing_exit_checker import TimingExitMixin
from .timing_lifecycle import TimingLifecycleMixin
from .timing_stop_loss import TimingStopLossMixin



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
        "max_reentries": 0,               # 已废弃：再入场不限制次数
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


from .timing_utils import (  # noqa: F401
    _deep_merge, _number_or_none, _nested_config,
    _breakout_day_low, _prev_day_low,
    entry_buypoint, entry_main_tier_price,
    _index_cumulative_metrics, _volume_pattern_not_distribution,
    CHASE_STRATEGIES, DIP_STRATEGIES,
)



class TimingEngine(TimingDataFetchMixin, TimingEntryMixin, TimingExitMixin,
                   TimingLifecycleMixin, TimingStopLossMixin):
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
                index_metrics = _index_cumulative_metrics(idx_records)
                if index_metrics:
                    data["index_cum_drop_5d"] = index_metrics["cum_drop_5d"]
                    data["index_down_days_5d"] = index_metrics["down_days_5d"]
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
            index_metrics = _index_cumulative_metrics(idx)
            if index_metrics:
                data["index_cum_drop_5d"] = index_metrics["cum_drop_5d"]
                data["index_down_days_5d"] = index_metrics["down_days_5d"]
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

        # 【P1-2】再入场额度提示：失败终态只降透明度，不直接剥夺新事件入场。
        # 用尽后仍允许调度，但必须显式披露已用/剩余，避免读者误判为新机会。
        try:
            from .signal_lifecycle import reentry_usage
            reentry_cfg = self._cfg("reentry") or {}
            if reentry_cfg.get("enabled", True):
                usage = reentry_usage(stock_code, store=self._lifecycle.store)
                if usage["used"] > 0:
                    notice = (
                        f"⚠ 该标的有{usage['used']}次历史失败尝试；再入场不限次数，"
                        "完整触发条件成立后按新事件调度"
                    )
                    tech_data["reentry_notice"] = notice
                    logger.warning("%s %s", stock_code, notice)
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
        reentry_notice = str(tech_data.get("reentry_notice") or "")
        if reentry_notice:
            for sig in merged:
                sig.trigger_reason = (
                    str(sig.trigger_reason).rstrip() + f"\n  {reentry_notice}"
                )

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
        vote_reasons = [
            str(detail).strip()
            for detail in (tech.get("vote_details") or [])
            if str(detail).strip()
        ]
        if vote_reasons:
            lines.append(f"  ↳投票依据: {'；'.join(vote_reasons)}")

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
