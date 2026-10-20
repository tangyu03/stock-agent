"""Exit signal checker for TimingEngine (mixin class)."""
import logging
from typing import Dict, Any, Optional, List


from .timing_models import ExitSignal


logger = logging.getLogger(__name__)


class TimingExitMixin:
    """Exit signal checker mixin for TimingEngine."""
    def check_exit_signals(
        self,
        stock_code: str,
        stock_name: str,
        market_mode: str = "defend",
        sector_status: str = "rotational",
        sector_name: str = "",
        sector_change_pct: Optional[float] = None,
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
        if sector_change_pct is not None:
            tech_data["sector_change_pct"] = float(sector_change_pct)
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

        # 指标走弱信息提前计算（供破位定性参考，不再独立产出卖出信号）
        weakness_labels = []
        _tech_sig = tech_data.get("tech_signals") or {}
        _vote = _tech_sig.get("vote_score")
        if _vote is not None and float(_vote) <= -1.0:
            weakness_labels.append(f"技术票{float(_vote):+.1f}")
        _boll = _tech_sig.get("bollinger") or {}
        if _boll.get("lower") and current_price and float(current_price) < float(_boll["lower"]):
            weakness_labels.append("跌破布林下轨")
        
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
            
            # 三问定性：放量/缩量 × 主动/被动 × 有无反抽
            vol_label = "缩量"
            if vol_ratio is not None and vol_ratio > heavy_vol_thresh:
                vol_label = "放量"
            elif vol_ratio is not None and vol_ratio > 0.8:
                vol_label = "正常量能"
            
            # 主动/被动：个股跌幅 vs 板块跌幅
            sector_change = tech_data.get("sector_change_pct")
            stock_change = tech_data.get("change_pct")
            is_active_break = None
            passive_note = ""
            if sector_change is not None and stock_change is not None:
                try:
                    if abs(float(stock_change)) < abs(float(sector_change)) * 0.8:
                        is_active_break = False
                        passive_note = "被动破位(被板块带崩)"
                    else:
                        is_active_break = True
                except (TypeError, ValueError):
                    pass
            if is_active_break is True:
                active_label = "主动"
            elif is_active_break is False:
                active_label = "被动"
            else:
                active_label = "口径未知"
            
            # 组装三问标签
            qual_label = f"{vol_label}+{active_label}"
            if passive_note:
                qual_label += f"({passive_note})"
            
            if paired_position:
                reason = (f'跌破配对止损Z={stop_loss_calc.stop_loss_price:.2f}'
                          f'（[{paired_strategy}]买入理由的直接否定，X 被证伪）')
            else:
                reason = f'跌破{stop_loss_calc.chosen_support:.2f}(止损{stop_loss_calc.stop_loss_price:.2f})'
            
            # 三档分流：
            # 档1: 放量+主动 → 紧急，立即触发
            # 档2: 正常量能+主动 或 放量+被动 → 紧急，立即触发
            # 档3: 缩量 或 被动+正常量能 → 重要，标注"待收盘确认"
            is_high_confidence_break = (
                (vol_label in ("放量", "正常量能") and is_active_break)
                or (vol_label == "放量" and not is_active_break)
            )
            
            reason += f'【{qual_label}】'
            
            if current_price > 0 and stop_loss_calc.stop_loss_price > 0:
                stop_gap_pct = (
                    (current_price - stop_loss_calc.stop_loss_price) / current_price * 100
                )
                reason += f'，距止损{stop_gap_pct:.1f}%'
            
            if is_high_confidence_break:
                urgency = '紧急'
                exit_label = '破位止损'
                reason += '，立即执行'
            else:
                urgency = '重要'
                exit_label = '破位预警'
                reason += '，待收盘确认'
            
            if vol_ratio is not None:
                reason += f'(量比{vol_ratio:.2f})'
            reason += f'，{self._volume_context(tech_data)}'
            if weakness_labels:
                reason += '；指标佐证:' + '/'.join(weakness_labels)
            if is_bearish:
                reason += f'；{tech_vote}(score={tech_score:+.1f})共振'
            bpats = [p.get('pattern','') for p in kline_patterns if '看跌' in p.get('signal','') or '压力' in p.get('signal','')]
            if bpats:
                reason += '; K线:' + ','.join(bpats)
            signals.append(ExitSignal(
                stock_code=stock_code, stock_name=stock_name, exit_type=exit_label,
                trigger_price=current_price, stop_loss_price=stop_loss_calc.stop_loss_price,
                reason=reason, urgency=urgency, mode_constrained=False,
                sector_status=sector_status, sector_name=sector_name, tech_data=tech_data,
            ))
            logger.info(
                '破位(%s) %s: %.2f≤止损%.2f，量比%.2f %s',
                qual_label, stock_code, current_price, stop_loss_calc.stop_loss_price,
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
            weakness.append((
                'strong',
                f'{tech_vote}({tech_score:+.1f})'
                f'{self._tech_vote_reason_suffix(tech_data)}'
            ))
        elif tech_vote == '偏空':
            weakness.append((
                'medium',
                f'{tech_vote}({tech_score:+.1f})'
                f'{self._tech_vote_reason_suffix(tech_data)}'
            ))
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
            buyback_ref = ma10 or ma20
            buyback_note = (
                f' | 接回参考MA10={buyback_ref:.2f}' if buyback_ref and buyback_ref > 0
                else ' | 接回参考:下级均线缺失'
            )
            signals.append(ExitSignal(
                stock_code=stock_code, stock_name=stock_name,
                exit_type='MA5压制', trigger_price=current_price,
                stop_loss_price=trailing_stop,
                reason=ma5_pressure_signal + ' | 减半+trailing(' + f'{trailing_stop:.2f}' + ')' + buyback_note,
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

        # 3. 技术走弱 — 降级为附加信息，不再独立产出卖出信号
        # （重塑：出场只看价格级别+量能定性，指标信号作为破位 reason 的补充）
        # 指标走弱信息存储在 weakness_labels 供破位信号引用
        weakness_labels = []
        if weakness:
            weakness_labels = [f'{lbl}({lvl})' for lvl, lbl in weakness]


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

