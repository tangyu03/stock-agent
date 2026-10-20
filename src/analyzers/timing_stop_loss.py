"""Stop loss calculation for TimingEngine (mixin class)."""
import logging
from typing import Dict, Any, Optional, List


from .timing_models import StopLossCalc


logger = logging.getLogger(__name__)


class TimingStopLossMixin:
    """Stop loss calculation mixin for TimingEngine."""
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
        stop_loss_multiplier = self._cfg("stop_loss", "multiplier", default=1.0)
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

