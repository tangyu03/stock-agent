"""
多模式自适应（回测版 + 大小盘风格轮动 + 双创技术位 + 外围扰动）

回测时按日重算操作模式，避免前视偏差。
不用 market_scorer 的实时 API，改用历史指数 K 线 + 简化规则。

规则（5维度）：
  score = (
      0.20 × index_ma_trend      # 上证趋势：站上MA20=1，附近=0.5，跌破=0
      + 0.20 × ma_alignment      # 均线排列：多头=1，粘合=0.5，空头=0
      + 0.20 × volume_score      # 成交量：量比>1.2=1，>0.8=0.5，<0.8=0
      + 0.20 × breadth_score     # 市场宽度：用指数涨幅代理，>1%=1，0-1%=0.5，<0%=0
      + 0.20 × gem_sci_tech_score # 双创技术位：强势共振=1，震荡=0.5，下跌中继=0
  ) × 10

  score >= 7 → attack（进攻）
  score 4-7  → defend（防守）
  score < 4  → retreat（撤退）

外围扰动降级：
  严重扰动（VIX>30/美股跌>2%）+ attack → defend；+ defend → retreat
  中度扰动（VIX>25/美股跌>1%）+ attack → defend

风格轮动（移植自 xiapi-style-rotation skill）：
  spread = 中证2000涨跌幅 - 沪深300涨跌幅
  spread > 2%  → 小盘风格（游资主导）
  spread < -2% → 大盘风格（机构主导）
  else         → 均衡
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 风格差值阈值（移植自 xiapi-style-rotation 的字段说明）
STYLE_THRESHOLDS = {
    "strong_small": 5.0,   # >5% 小盘明显强
    "small": 2.0,          # 2-5% 小盘较强
    "tilt_small": 0.0,     # 0-2% 偏小盘
    "tilt_large": -2.0,    # -2-0% 偏大盘
    "large": -5.0,         # -5~-2% 大盘较强
    "strong_large": -5.0,  # <-5% 大盘明显强
}


def calc_style_spread(csi2000_kline: List[Dict] = None, csi300_kline: List[Dict] = None) -> Dict:
    """
    计算大小盘风格差值（纯本地计算，替代 daxiapi-cli）

    spread = 中证2000当日涨跌幅 - 沪深300当日涨跌幅

    数据源：AKShare 指数日K线（代码 932000=中证2000, 000300=沪深300）

    Returns:
        {
            "spread": float,              # 当日差值（%）
            "spread_5d": float,           # 5日累计差值（%）
            "style": "小盘"|"大盘"|"均衡",
            "style_strength": "明显"|"较强"|"略强"|"",
            "trend": "上升"|"下降"|"震荡",  # 近5日差值趋势
            "csi2000_change": float,       # 中证2000涨跌幅
            "csi300_change": float,        # 沪深300涨跌幅
            "mean_reversion_risk": "高"|"中"|"低",  # 均值回归风险
        }
    """
    result = {"spread": 0.0, "style": "均衡", "trend": "震荡",
              "csi2000_change": 0.0, "csi300_change": 0.0}

    try:
        import akshare as ak

        # 获取中证2000指数K线
        if csi2000_kline is None:
            df2000 = ak.stock_zh_index_daily(symbol="sh932000")
            if df2000 is None or len(df2000) < 2:
                return result
            csi2000_recent = df2000.tail(21)
        else:
            csi2000_recent = csi2000_kline[-21:] if len(csi2000_kline) >= 21 else csi2000_kline

        # 获取沪深300指数K线
        if csi300_kline is None:
            df300 = ak.stock_zh_index_daily(symbol="sh000300")
            if df300 is None or len(df300) < 2:
                return result
            csi300_recent = df300.tail(21)
        else:
            csi300_recent = csi300_kline[-21:] if len(csi300_kline) >= 21 else csi300_kline

        if len(csi2000_recent) < 2 or len(csi300_recent) < 2:
            return result

        # 计算每日涨跌幅和差值
        spreads = []
        for i in range(1, min(len(csi2000_recent), len(csi300_recent))):
            c2000 = float(csi2000_recent.iloc[i]["close"])
            c2000_prev = float(csi2000_recent.iloc[i - 1]["close"])
            c300 = float(csi300_recent.iloc[i]["close"])
            c300_prev = float(csi300_recent.iloc[i - 1]["close"])

            chg2000 = (c2000 - c2000_prev) / c2000_prev * 100
            chg300 = (c300 - c300_prev) / c300_prev * 100
            spreads.append(chg2000 - chg300)

        if not spreads:
            return result

        spread = spreads[-1]
        spread_5d = sum(spreads[-5:]) if len(spreads) >= 5 else sum(spreads)

        # 风格判断
        if spread > STYLE_THRESHOLDS["strong_small"]:
            style, strength = "小盘", "明显"
        elif spread > STYLE_THRESHOLDS["small"]:
            style, strength = "小盘", "较强"
        elif spread > STYLE_THRESHOLDS["tilt_small"]:
            style, strength = "小盘", "略强"
        elif spread > STYLE_THRESHOLDS["tilt_large"]:
            style, strength = "大盘", "略强"
        elif spread > STYLE_THRESHOLDS["large"]:
            style, strength = "大盘", "较强"
        else:
            style, strength = "大盘", "明显"

        # 趋势判断（近5日差值的线性回归斜率）
        recent_5 = spreads[-5:] if len(spreads) >= 5 else spreads
        if len(recent_5) >= 3:
            n = len(recent_5)
            x_avg = (n - 1) / 2
            y_avg = sum(recent_5) / n
            num = sum((i - x_avg) * (recent_5[i] - y_avg) for i in range(n))
            den = sum((i - x_avg) ** 2 for i in range(n))
            slope = num / den if den != 0 else 0
            if slope > 0.05:
                trend = "上升"
            elif slope < -0.05:
                trend = "下降"
            else:
                trend = "震荡"
        else:
            trend = "震荡"

        # 均值回归风险：差值越极端，回归风险越高
        abs_spread = abs(spread)
        if abs_spread > 8:
            reversion = "高"
        elif abs_spread > 5:
            reversion = "中"
        else:
            reversion = "低"

        result = {
            "spread": round(spread, 2),
            "spread_5d": round(spread_5d, 2),
            "style": style,
            "style_strength": strength,
            "trend": trend,
            "csi2000_change": round(spreads[-1] if spreads else 0, 2),
            "csi300_change": round(0.0, 2),  # can't separate from spread alone
            "mean_reversion_risk": reversion,
        }
    except Exception as e:
        logger.debug("风格差值计算失败（非关键，降级到均衡）: %s", e)

    return result


class MarketModeAdaptive:
    """多模式自适应 + 风格轮动"""

    def __init__(self):
        # 缓存指数 K 线，避免重复拉取
        self._index_cache: Dict[str, List[Dict]] = {}
        # 风格差值缓存（当日仅算一次）
        self._style_spread: Optional[Dict] = None
        self._style_date: Optional[str] = None
        # 双创技术位缓存（当日仅算一次）
        self._gem_sci_tech_result: Optional[Dict] = None
        self._gem_sci_tech_date: Optional[str] = None

    # ================================================================
    # 大盘环境评估（真实数据驱动，不虚构评分）
    # ================================================================

    @staticmethod
    def _count_distribution_days(kline_data: List[Dict], window: int = 25) -> Dict:
        """
        统计滚动窗口内的派发日数量（IBD 市场暴露模型核心指标）

        派发日定义：收盘跌 >0.2% 且成交量 > 前一日成交量。
        机构在放量下跌日出货，散户在缩量下跌日抛售——前者才是真正的派发信号。
        """
        recent = kline_data[-window:] if len(kline_data) >= window else kline_data
        dist_days = []

        for i in range(1, len(recent)):
            prev = recent[i - 1]
            curr = recent[i]
            prev_close = float(prev.get("close", 0))
            curr_close = float(curr.get("close", 0))
            prev_vol = float(prev.get("volume", 0))
            curr_vol = float(curr.get("volume", 0))

            if prev_close <= 0 or curr_close <= 0:
                continue

            change_pct = (curr_close - prev_close) / prev_close * 100

            if change_pct < -0.2 and prev_vol > 0 and curr_vol > prev_vol:
                dist_days.append({
                    "date": str(curr.get("date", ""))[:10],
                    "change_pct": round(change_pct, 2),
                    "vol_ratio": round(curr_vol / prev_vol, 2) if prev_vol > 0 else 0,
                })

        return {
            "count": len(dist_days),
            "window": len(recent),
            "days": dist_days,
        }

    @staticmethod
    def _get_advance_decline() -> Dict:
        """
        获取真实市场宽度：涨跌家数比（AKShare 乐咕接口）

        Returns:
            {"advance": int, "decline": int, "flat": int, "ratio": float, "available": bool}
        """
        result = {"advance": 0, "decline": 0, "flat": 0, "ratio": 1.0, "available": False}
        try:
            import akshare as ak
            df = ak.stock_market_activity_legu()
            if df is not None and not df.empty:
                stats = {}
                for _, row in df.iterrows():
                    stats[str(row.get("item", ""))] = row.get("value", 0)
                advance = int(float(stats.get("上涨", 0)))
                decline = int(float(stats.get("下跌", 0)))
                flat = int(float(stats.get("平盘", 0)))
                result = {
                    "advance": advance,
                    "decline": decline,
                    "flat": flat,
                    "ratio": round(advance / decline, 2) if decline > 0 else float("inf"),
                    "available": True,
                }
        except Exception as e:
            logger.debug("涨跌家数获取失败（非关键）: %s", e)
        return result

    def _assess_market(self, date: str, index_kline: List[Dict]) -> Dict:
        """
        大盘环境评估：真实数据 + IBD 派发日模型 + 规则判定模式。

        输出均为客观数据描述，不再虚构 0-10 评分。

        Returns:
            {
                "dimensions": [
                    {"key": "index_trend", "name": "指数趋势", "condition": "站上MA20(3321>3285) | 站上MA50(3321>3250)", "status": "bullish"},
                    ...
                ],
                "dist_days": {"count": 3, "window": 25, ...},
                "advance_decline": {...},
                "mode": "attack"|"defend"|"retreat",
                "mode_reason": "派发日3/25<5 | 站上MA50 | 多头排列",
            }
            数据不足时返回 None
        """
        if len(index_kline) < 30:
            return None

        today_kline = None
        for i, k in enumerate(index_kline):
            if str(k.get("date", ""))[:10] == str(date)[:10]:
                today_kline = k
                history = index_kline[: i + 1]
                break
        if not today_kline:
            history = [k for k in index_kline if str(k.get("date", ""))[:10] <= str(date)[:10]]
            if not history:
                return None
            today_kline = history[-1]

        if len(history) < 30:
            return None

        closes = [float(k["close"]) for k in history]
        volumes = [float(k.get("volume", 0)) for k in history]

        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma20
        current = closes[-1]

        dimensions = []

        # --- 维度 1: 指数趋势（价格 vs MA20/MA50） ---
        if current > ma20 * 1.01:
            vs_ma20, op20 = "站上", ">"
        elif current > ma20 * 0.99:
            vs_ma20, op20 = "MA20附近", "≈"
        else:
            vs_ma20, op20 = "跌破", "<"
        if current > ma50 * 1.01:
            vs_ma50, op50 = "站上", ">"
        elif current > ma50 * 0.99:
            vs_ma50, op50 = "MA50附近", "≈"
        else:
            vs_ma50, op50 = "跌破", "<"
        idx_cond = f"{vs_ma20}MA20({current:.0f}{op20}{ma20:.0f}) | {vs_ma50}MA50({current:.0f}{op50}{ma50:.0f})"
        idx_status = "bullish" if current > ma20 and current > ma50 else ("bearish" if current < ma20 and current < ma50 else "neutral")
        dimensions.append({"key": "index_trend", "name": "指数趋势", "condition": idx_cond, "status": idx_status})

        # --- 维度 2: 均线排列 ---
        if ma5 > ma10 > ma20:
            ma_cond = f"多头排列(MA5 {ma5:.0f}>MA10 {ma10:.0f}>MA20 {ma20:.0f})"
            ma_status = "bullish"
        elif ma5 > ma10 or ma10 > ma20:
            ma_cond = "交叉震荡"
            ma_status = "neutral"
        else:
            ma_cond = "空头排列"
            ma_status = "bearish"
        dimensions.append({"key": "ma_alignment", "name": "均线排列", "condition": ma_cond, "status": ma_status})

        # --- 维度 3: 量价确认 ---
        avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
        today_vol = volumes[-1] if volumes else 0
        vol_cond = "无数据"
        vol_status = "neutral"
        if avg_vol_20 > 0:
            vol_ratio = today_vol / avg_vol_20
            if vol_ratio > 1.2:
                vol_cond = f"放量 {vol_ratio:.1f}x"
                if len(closes) >= 2 and current > closes[-2]:
                    vol_status = "bullish"
                else:
                    vol_status = "neutral"
            elif vol_ratio > 0.8:
                vol_cond = f"量平 {vol_ratio:.1f}x"
                vol_status = "neutral"
            else:
                vol_cond = f"缩量 {vol_ratio:.1f}x"
                vol_status = "bearish"
        dimensions.append({"key": "volume", "name": "量价确认", "condition": vol_cond, "status": vol_status})

        # --- 维度 4: 市场宽度（真实涨跌家数比） ---
        ad = self._get_advance_decline()
        if ad["available"]:
            if ad["ratio"] > 1.5:
                b_status = "bullish"
            elif ad["ratio"] < 0.8:
                b_status = "bearish"
            else:
                b_status = "neutral"
            b_cond = f"涨{ad['advance']}跌{ad['decline']}平{ad['flat']} (涨跌比 {ad['ratio']}:1)"
        else:
            b_cond = "数据不可用"
            b_status = "neutral"
        dimensions.append({"key": "breadth", "name": "市场宽度", "condition": b_cond, "status": b_status, "advance_decline": ad})

        # --- 维度 5: 派发日统计（IBD 核心指标） ---
        dist = self._count_distribution_days(history)
        if dist["count"] <= 4:
            d_status = "bullish"
        elif dist["count"] <= 6:
            d_status = "neutral"
        else:
            d_status = "bearish"
        d_cond = f"派发日 {dist['count']}/{dist['window']}"
        dimensions.append({"key": "dist_days", "name": "派发日统计", "condition": d_cond, "status": d_status, "dist_detail": dist})

        # --- 双创技术位（展示但不参与模式判定） ---
        gem_analysis = self.get_gem_sci_tech_analysis()
        gem_cond = gem_analysis.get("trend_judgment", "未知") if gem_analysis else "未知"
        dimensions.append({"key": "gem_sci_tech", "name": "双创技术", "condition": gem_cond, "status": "neutral"})

        # ================================================================
        # 模式判定：B1 五日线回归 v2（2026-07-22 整改，含缓冲优化）
        # ----------------------------------------------------------------
        # 主基准：指数5日线（帖16"指数锚定5日"是绝对原则）
        #   - 收盘 < MA5×0.99（跌破1%缓冲） → retreat（收缩）
        #   - 收盘 > MA5 且 多头排列 且 派发日≤5 → attack（激情）
        #   - 其他（站上5日线但不满足激情条件） → defend（谨慎）
        # v2 改动（v1 问题：模式切换太频繁30%，attack平均仅3.7天）：
        #   1. retreat 用 1% 缓冲，避免日内毛刺触发
        #   2. attack 去掉"5日仍上升"条件（震荡市5日线方向频繁变化）
        # 派发日降级为辅助参考（不再作硬条件）
        # ================================================================
        dist_count = dist["count"]
        above_ma5 = current > ma5
        below_ma5_buffer = current < ma5 * 0.99  # 1%缓冲
        ma_bullish = ma5 > ma10 > ma20
        above_ma50 = current > ma50  # 仅作展示

        reasons = []
        if below_ma5_buffer:
            # 收盘跌破 MA5 1% → 收缩（带缓冲，避免毛刺）
            mode_before = "retreat"
            reasons.append(f"跌破5日线1%缓冲(价{current:.0f}<MA5×0.99:{ma5*0.99:.0f})")
            if dist_count >= 7:
                reasons.append(f"派发日{dist_count}/{dist['window']}≥7 严重派发(辅助)")
        elif above_ma5 and ma_bullish and dist_count <= 5:
            # 激情档：站上5日线 + 多头排列 + 派发日不严重
            mode_before = "attack"
            reasons.append(f"站上5日线(价{current:.0f}>MA5:{ma5:.0f})")
            reasons.append("多头排列(MA5>MA10>MA20)")
            if dist_count <= 4:
                reasons.append(f"派发日{dist_count}/{dist['window']}≤4(辅助)")
        else:
            # 谨慎档：站上5日线但不满足激情条件（含5日线附近震荡）
            mode_before = "defend"
            reasons.append(f"5日线附近(价{current:.0f}≈MA5:{ma5:.0f})")
            if not ma_bullish:
                reasons.append("均线未多头排列")
            if dist_count >= 5:
                reasons.append(f"派发日{dist_count}/{dist['window']}≥5(辅助)")

        # 外围扰动降级（仅实时）
        from datetime import datetime as dt
        today_str = dt.now().strftime("%Y-%m-%d")
        mode = mode_before
        shock_downgraded = False
        if date == today_str:
            mode_after = self._apply_external_shock(mode_before)
            if mode_after != mode_before:
                shock_downgraded = True
                reasons.append(f"外盘扰动: {mode_before}→{mode_after}")
                mode = mode_after

        return {
            "dimensions": dimensions,
            "dist_days": dist,
            "advance_decline": ad,
            "mode_before_shock": mode_before,
            "mode": mode,
            "shock_downgraded": shock_downgraded,
            "mode_reason": " | ".join(reasons),
        }

    def score_dimensions(self, date: str, index_kline: List[Dict]) -> Dict:
        """兼容旧接口，委托给 _assess_market"""
        result = self._assess_market(date, index_kline)
        if result is None:
            return None
        # 为调用方提供兼容的 raw_score / dim_sum 字段
        dims = result.get("dimensions", [])
        bullish = sum(1 for d in dims if d.get("status") == "bullish")
        neutral = sum(1 for d in dims if d.get("status") == "neutral")
        # 兼容：用 bullish 占比估算，范围 0-10
        compat_score = min(10, (bullish * 2.0 + neutral * 1.0))
        result["raw_score"] = round(compat_score, 1)
        result["dim_sum"] = round(compat_score / 2.0, 1)
        return result


    def get_mode_for_date(self, date: str, index_kline: List[Dict]) -> str:
        """
        根据日期计算操作模式（委托给 _score_dimensions）

        Args:
            date: 当前日期 YYYY-MM-DD
            index_kline: 上证指数 K 线（截止到 date 的，不含 date 之后）

        Returns:
            "attack" / "defend" / "retreat"
        """
        result = self.score_dimensions(date, index_kline)
        if result is None:
            return "defend"
        return result["mode"]

    def get_style_spread(self, force_refresh: bool = False) -> Dict:
        """
        获取当日大小盘风格差值（带缓存，当日仅调一次 AKShare）

        Returns:
            calc_style_spread() 的结果字典
        """
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if not force_refresh and self._style_date == today and self._style_spread:
            return self._style_spread
        self._style_spread = calc_style_spread()
        self._style_date = today
        return self._style_spread

    def get_gem_sci_tech_analysis(self, force_refresh: bool = False) -> Dict:
        """
        获取当日双创技术位分析（带缓存，当日仅算一次）

        Returns:
            analyze_gem_sci_tech() 的结果字典
        """
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if not force_refresh and self._gem_sci_tech_date == today and self._gem_sci_tech_result:
            return self._gem_sci_tech_result
        try:
            from ..analyzers.gem_sci_tech_scorer import analyze_gem_sci_tech
            self._gem_sci_tech_result = analyze_gem_sci_tech()
            self._gem_sci_tech_date = today
        except Exception as e:
            logger.warning("双创技术位分析失败: %s", e)
            self._gem_sci_tech_result = {"trend_judgment": "数据不足", "risk_flag": "none"}
            self._gem_sci_tech_date = today
        return self._gem_sci_tech_result

    def _get_gem_sci_tech_score(self, date: str) -> float:
        """
        获取双创技术位维度分 (0.0-1.0)，用于 get_mode_for_date

        Args:
            date: 目标日期 YYYY-MM-DD

        Returns:
            0.0-1.0 的维度分
        """
        from datetime import datetime as dt
        today_str = dt.now().strftime("%Y-%m-%d")

        # 回测模式：当日不拉双创数据（避免前视偏差），给中性分
        if date != today_str:
            return 0.5

        # 实时模式：拉取双创数据
        try:
            from ..analyzers.gem_sci_tech_scorer import gem_sci_tech_to_mode_score
            analysis = self.get_gem_sci_tech_analysis()
            trend = analysis.get("trend_judgment", "震荡分化")
            return gem_sci_tech_to_mode_score(trend)
        except Exception as e:
            logger.warning("双创维度评分失败，回退到中性: %s", e)
            return 0.5

    @staticmethod
    def _apply_external_shock(mode: str) -> str:
        """
        根据外围扰动等级对模式做降级

        Args:
            mode: 当前模式 "attack" / "defend" / "retreat"

        Returns:
            降级后的模式
        """
        try:
            from ..analyzers.external_market import get_external_market_assessment, apply_external_downgrade
            assessment = get_external_market_assessment()
            disturbance = assessment.get("disturbance", {})
            if disturbance:
                level_code = disturbance.get("level_code", 0)
                if level_code >= 2:
                    return apply_external_downgrade(mode, level_code)
        except Exception as e:
            logger.debug("外围扰动降级检查跳过: %s", e)
        return mode

    # ================================================================
    # 综合环境评估（供盘前/盘中调用）
    # ================================================================

    def _fetch_index_kline(self) -> List[Dict]:
        """获取上证指数日K线（P1-14: 优先用AKShareAdapter，fallback直调）"""
        # 优先走 adapter（带超时+熔断）
        try:
            from ..data_layer.akshare_adapter import get_akshare_adapter
            adapter = get_akshare_adapter()
            result = adapter.get_index_data("000001")
            if result.success and result.data:
                return [
                    {"date": str(row.get("date", ""))[:10],
                     "close": float(row.get("close", 0)),
                     "volume": float(row.get("volume", 0))}
                    for row in result.data
                ]
        except Exception as e:
            logger.warning("指数K线获取失败(adapter): %s", e)
        # fallback: 直接调 akshare
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol="sh000001")
            if df is not None and len(df) > 0:
                return [
                    {"date": str(row["date"])[:10], "close": float(row["close"]),
                     "volume": float(row.get("volume", 0))}
                    for _, row in df.iterrows()
                ]
        except Exception as e:
            logger.warning("指数K线获取失败(akshare直调): %s", e)
        return []

    def assess_daily(self, force_refresh: bool = False) -> Dict:
        """
        综合环境评估 — 一次调用获取所有盘前/盘中所需环境数据。

        包含：自适应市场模式（含外盘降级+双创评分）、双创技术位详情、
        大小盘风格轮动、外围市场扰动、5维评分推导链。

        Returns:
            {
                "mode": "attack"|"defend"|"retreat",
                "score": float (0-10),        # 真实 5 维评分（非近似）
                "position_limit": float,
                "dimensions": [...],           # 5 维评分推导链
                "dim_sum": float,              # 5 维原始分之和
                "mode_before_shock": str,      # 外盘降级前的模式
                "shock_downgraded": bool,      # 是否被外盘降级
                "gem_sci_tech": dict or None,
                "external_market": dict or None,
                "style_spread": dict or None,
            }
        """
        from datetime import datetime as dt
        today = dt.now().strftime("%Y-%m-%d")

        # 1. 获取指数K线 + 5维评分
        index_kline = self._fetch_index_kline()
        scoring = self.score_dimensions(today, index_kline) if index_kline else None

        if scoring:
            mode = scoring["mode"]
            score = scoring["raw_score"]
            dimensions = scoring["dimensions"]
            dim_sum = scoring["dim_sum"]
            mode_before_shock = scoring["mode_before_shock"]
            shock_downgraded = scoring["shock_downgraded"]
        else:
            mode = "defend"
            score = 5.0
            dimensions = []
            dim_sum = 2.5
            mode_before_shock = "defend"
            shock_downgraded = False

        # 2. 展示数据（双创详情、风格轮动、外围扰动）— 并行拉取，各自独立超时
        from concurrent.futures import ThreadPoolExecutor, as_completed
        gem_sci_tech = None
        style_spread = None
        external_market = None

        def _fetch_gem():
            return self.get_gem_sci_tech_analysis(force_refresh)

        def _fetch_style():
            return self.get_style_spread(force_refresh)

        def _fetch_external():
            try:
                from ..analyzers.external_market import get_external_market_assessment
                return get_external_market_assessment()
            except Exception as e:
                logger.debug("外围市场评估跳过: %s", e)
                return None

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_fetch_gem): "gem",
                pool.submit(_fetch_style): "style",
                pool.submit(_fetch_external): "external",
            }
            for future in as_completed(futures, timeout=60):
                try:
                    result = future.result(timeout=30)
                    key = futures[future]
                    if key == "gem":
                        gem_sci_tech = result
                    elif key == "style":
                        style_spread = result
                    elif key == "external":
                        external_market = result
                except Exception as e:
                    logger.warning("%s 数据拉取失败: %s", futures[future], e)

        # 3. 仓位上限
        position_limit = {"attack": 0.8, "defend": 0.5, "retreat": 0.1}.get(mode, 0.5)

        # 4. 市场环境增强指标（成交量分位/涨跌家数/沪深300）
        market_env = None
        try:
            from ..analyzers.market_env import get_market_environment
            market_env = get_market_environment(force_refresh)
        except Exception as e:
            logger.debug("市场环境增强指标跳过: %s", e)

        return {
            "mode": mode,
            "score": score,
            "position_limit": position_limit,
            "dimensions": dimensions,
            "dim_sum": dim_sum,
            "mode_before_shock": mode_before_shock,
            "shock_downgraded": shock_downgraded,
            "gem_sci_tech": gem_sci_tech,
            "external_market": external_market,
            "style_spread": style_spread,
            "market_env": market_env,
        }

    def get_mode_series(self, index_kline: List[Dict]) -> Dict[str, str]:
        """
        一次性算出整个时间窗口的每日模式

        Args:
            index_kline: 完整指数 K 线

        Returns:
            {date: mode} 映射
        """
        mode_series = {}
        for i, k in enumerate(index_kline):
            date = k["date"]
            history = index_kline[: i + 1]
            mode = self.get_mode_for_date(date, history)
            mode_series[date] = mode
        return mode_series


# 单例
_instance: Optional[MarketModeAdaptive] = None


def get_market_mode_adaptive() -> MarketModeAdaptive:
    global _instance
    if _instance is None:
        _instance = MarketModeAdaptive()
    return _instance
