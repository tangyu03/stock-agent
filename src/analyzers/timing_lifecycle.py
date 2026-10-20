"""Signal event lifecycle for TimingEngine (mixin class)."""
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from .timing_utils import _number_or_none, _volume_pattern_not_distribution


logger = logging.getLogger(__name__)


class TimingLifecycleMixin:
    """Signal event lifecycle mixin for TimingEngine."""
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
                capitulation_evidence=bool(
                    tech_data.get("long_lower_shadow")
                    or tech_data.get("shrinking_stabilize")
                    or tech_data.get("has_hammer")
                    or _volume_pattern_not_distribution(tech_data)
                ),
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

