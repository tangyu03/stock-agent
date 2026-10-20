"""Entry strategy checkers for TimingEngine (mixin class)."""
import logging
from typing import Dict, Any, Optional, List, Tuple


from .timing_models import EntrySignal
from ..analyzers.market_env import get_market_environment
from ..analyzers.stock_filter import get_stock_filter, FilterResult
from .timing_utils import _number_or_none


logger = logging.getLogger(__name__)


class TimingEntryMixin:
    """Entry strategy checker mixin for TimingEngine."""
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
        A 增补（2026-10-15）：多日单边阴跌计入恐慌（上证近5日累计跌幅 +
            连续收阴天数），匹配"破位后持续下行"的真实体感。
        B 修复（2026-10-15）：关键整数关口改用上证指数收盘/盘中（原误用
            沪深300 csi300_close），并新增"盘中破位+尾盘未收回"降级充分条件。
        制度前提：恐慌抄底依赖"恐慌必有托底"的政策预期（A股特色）
        在无政策托底环境（如2021-2023纯熊市）降级为"考虑"级信号

        触发条件（必要+充分）：
        必要条件（满足任一）：
          - 上证跌幅 > 4%（保留，作必要不充分）
          - 双创跌幅 > 5%
          - 涨跌比 < 0.15
          - 上证近5日累计跌幅 > 6% 且 连续收阴 ≥ 3 日（A）
        充分条件（必要条件满足后，再满足任一）：
          - 关键整数关口破位：上证收盘跌破 3000/3100/3200/.../4000 等100整百点位（B：上证收盘口径）
          - 盘中破位+尾盘未收回：上证盘中跌破整数关口且收盘仍在关口下方（B：降级充分）
          - 放量宣泄：成交量250日分位 > 70%
          - 极端放量：均量比 > 2.0 且上证跌 > 2%
          - 累计恐慌确认（A）：多日单边阴跌(必要已含) + 个股超卖 → 左侧试探放行
        """
        panic_market = []
        panic_sufficient = []  # D1: 充分条件
        cumulative_panic = False

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

        # === A：累计恐慌（多日单边阴跌）===
        cum_drop = tech_data.get("index_cum_drop_5d", 0) or 0
        down_days = tech_data.get("index_down_days_5d", 0) or 0
        cum_drop_thresh = self._cfg("panic_bottom", "cum_drop_5d_threshold", default=6.0)
        cum_days_thresh = self._cfg("panic_bottom", "cum_consecutive_down_days", default=3)
        if abs(cum_drop) > cum_drop_thresh and down_days >= cum_days_thresh:
            cumulative_panic = True
            panic_market.append(f"上证5日累跌{abs(cum_drop):.1f}%连{down_days}阴")

        if not panic_market:
            return None

        # === 个股超卖（提前算：累计恐慌降级放行需用它）===
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

        # === 反转结构（左侧信号：替代滞后确认，判定"跌完了"而非"跌很多"）===
        reversal_structure = []
        if tech_data.get("long_lower_shadow"):
            reversal_structure.append("长下影(尾盘拉回)")
        if tech_data.get("shrinking_stabilize"):
            reversal_structure.append("缩量企稳")
        if tech_data.get("has_hammer"):
            reversal_structure.append("锤子线")
        if (tech_data.get("long_lower_shadow") or tech_data.get("shrinking_stabilize")
                or tech_data.get("has_hammer")):
            if sector and str(sector).lower() not in ("retreating", "退潮"):
                reversal_structure.append("板块未退潮")

        if not stock_oversold and not reversal_structure:
            return None

        # === D1 充分条件1: 关键整数关口破位（B：上证指数收盘口径，不再用沪深300）===
        index_close = 0
        index_low = 0
        index_prev_close = 0
        try:
            idx_records = tech_data.get("index_kline") or ((self._market_cache or {}).get("index_kline") or [])
            if idx_records:
                last = idx_records[-1]
                index_close = float(last.get("close", last.get("收盘", 0)) or 0)
                index_low = float(last.get("low", last.get("最低", index_close)) or index_close)
                if len(idx_records) >= 2:
                    prev = idx_records[-2]
                    index_prev_close = float(prev.get("close", prev.get("收盘", 0)) or 0)
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        if index_close > 0:
            for level in range(3000, 4600, 100):
                prev_close = index_prev_close or (
                    index_close / (1 + index_drop / 100) if index_drop != 0 else index_close
                )
                if prev_close > level >= index_close:
                    panic_sufficient.append(f"上证跌破{level}关口(收{index_close:.0f})")
                    break
            # B：降级充分 — 盘中跌破整数关口 且 尾盘未收回（收盘仍在关口下方）
            if not panic_sufficient and index_low > 0:
                for level in range(3000, 4600, 100):
                    if index_low < level and index_close < level:
                        panic_sufficient.append(
                            f"盘中破{level}关口未收回(低{index_low:.0f}收{index_close:.0f})"
                        )
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

        # 缩量阴跌过滤：只拦"放量类"恐慌；多日阴跌路径（A）不因当日缩量被一刀切
        if any("不宜抄底" in p for p in panic_market) and not cumulative_panic:
            return None

        # === A：累计恐慌降级放行 — 多日单边阴跌 + 个股超卖 → 左侧试探 ===
        if not panic_sufficient and cumulative_panic:
            panic_sufficient.append(
                f"累计恐慌确认(5日累跌{abs(cum_drop):.1f}%连{down_days}阴)"
            )

        # D1: 必要条件（panic_market）+ 充分条件（panic_sufficient）双重确认
        # 原逻辑：必要条件≥1 即可触发（过松，2024段产生大量低质量信号）
        # 新逻辑：必要条件≥1 + 充分条件≥1 才触发
        # 如果没有充分条件，降级为"考虑"级信号（不触发买入，仅记录）
        if not panic_sufficient:
            # 无充分条件 — 降级，不触发
            return None

        # D1: 市场恐慌(必要)+充分条件+个股超卖 三重确认
        all_conds = panic_market + panic_sufficient + stock_oversold + reversal_structure
        trigger_reason = ";".join(all_conds)
        hc_market = self._cfg("panic_bottom", "high_confidence_market_conds", default=1)
        hc_stock = self._cfg("panic_bottom", "high_confidence_stock_conds", default=2)
        # D1: 高置信需要必要+充分+超卖都≥阈值
        confidence = "高" if (len(panic_market) >= hc_market and
                                  len(panic_sufficient) >= 1 and
                                  (len(stock_oversold) + len(reversal_structure)) >= hc_stock) else "中"
        # A：仅靠累计恐慌放行的左侧试探，置信度封顶"中"
        if (
            cumulative_panic
            and len(panic_sufficient) == 1
            and "累计恐慌确认" in panic_sufficient[0]
        ):
            confidence = "中"

        return EntrySignal(
            stock_code=code, stock_name=name, entry_type="恐慌抄底",
            strategy_summary="恐慌抄底 — 大盘恐慌(暴跌/放量/涨跌比极低/多日阴跌) + 个股超卖/反转结构(暴跌/锤子线/长下影/缩量企稳)",
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

    def _tech_vote_reason_suffix(self, tech_data: Dict[str, Any]) -> str:
        """把技术综合票展开到可复核的分组原因，禁止只给一个偏空分数。"""
        tech = tech_data.get("tech_signals", {})
        if not isinstance(tech, dict):
            return ""
        details = tech.get("vote_details") or []
        reasons = []
        for detail in details[:4]:
            text = str(detail or "").strip()
            if text:
                reasons.append(text)
        if not reasons:
            return ""
        return "；投票依据: " + "；".join(reasons)

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

