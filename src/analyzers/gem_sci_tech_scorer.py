"""
双创指数技术位分析器
对科创50(399688)和创业板指(399006)做独立技术位判断，产出综合标签。

核心能力：
- MA5/MA10/MA20/MA60/MA120 多级均线体系
- 多级关键位：三层防线（MA20短线 / MA60中期 / MA120牛熊），逐层判断攻防状态
- 量价配合：缩量止跌 vs 放量下杀 vs 下跌中继
- 综合标签：强势共振 / 分化 / 弱势共振 / 下跌中继 / 止跌企稳

纯本地计算，不消耗问财配额。
"""
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# 均线层级体系
# ============================================================

# 三个关键均线及其市场含义
MA_LEVELS = [
    {"key": "MA20",  "period": 20,  "name": "短线生命线", "weight": 0.3},
    {"key": "MA60",  "period": 60,  "name": "中期攻防线", "weight": 0.4},
    {"key": "MA120", "period": 120, "name": "牛熊分界线", "weight": 0.3},
]


def _compute_mas(closes: List[float]) -> Dict[str, float]:
    """计算多级均线值"""
    result = {}
    for level in MA_LEVELS:
        p = level["period"]
        if len(closes) >= p:
            result[level["key"]] = sum(closes[-p:]) / p
    # 短周期均线
    if len(closes) >= 5:
        result["MA5"] = sum(closes[-5:]) / 5
    if len(closes) >= 10:
        result["MA10"] = sum(closes[-10:]) / 10
    return result


def _ma_alignment(mas: Dict[str, float]) -> str:
    """均线排列：多头 / 空头 / 粘合"""
    ma5 = mas.get("MA5")
    ma10 = mas.get("MA10")
    ma20 = mas.get("MA20")
    if not all([ma5, ma10, ma20]):
        return "unknown"
    if ma5 > ma10 > ma20:
        return "bullish"
    elif ma5 < ma10 < ma20:
        return "bearish"
    else:
        max_ma = max(ma5, ma10, ma20)
        min_ma = min(ma5, ma10, ma20)
        spread = (max_ma - min_ma) / max_ma if max_ma > 0 else 0
        return "converging" if spread < 0.01 else "mixed"


def _single_ma_status(
    current: float,
    ma_value: Optional[float],
    closes: List[float],
    level_name: str,
) -> str:
    """
    单条均线的攻防状态

    Returns: above / near_above / near_below / below / just_broke_down / rebound_blocked / unknown
    """
    if ma_value is None or ma_value <= 0:
        return "unknown"

    deviation = (current - ma_value) / ma_value
    threshold = 0.02  # ±2% 以内视为"附近"

    if deviation > threshold:
        return "above"
    elif deviation > 0:
        return "near_above"
    elif deviation > -threshold:
        # 检查是否刚跌破
        if len(closes) >= 2:
            prev = closes[-2]
            prev_dev = (prev - ma_value) / ma_value
            if prev_dev > 0 and deviation < 0:
                return "just_broke_down"
        # 检查是否反弹受阻（3日内曾尝试站上但失败）
        if len(closes) >= 4:
            recent = closes[-4:-1]
            tried = any((c - ma_value) / ma_value > 0 for c in recent)
            if tried and deviation < 0:
                return "rebound_blocked"
        return "near_below"
    else:
        # 显著低于均线
        if len(closes) >= 2:
            prev = closes[-2]
            if prev > ma_value and current < ma_value:
                return "just_broke_down"
        return "below"


def _analyze_ma_levels(current: float, mas: Dict[str, float], closes: List[float]) -> Dict:
    """
    分析多级均线的整体攻防状态

    Returns:
        {
            "levels": {  # 逐层状态
                "MA20": {"status": "above", "deviation_pct": +3.2, "label": "站上"},
                "MA60": {"status": "below", "deviation_pct": -1.5, "label": "跌破"},
                "MA120": {"status": "above", "deviation_pct": +5.0, "label": "站上"},
            },
            "defense_depth": int,   # 还在支撑的层数（从短到长）
            "worst_level": str,     # 最差状态描述
            "summary": str,         # 一句话摘要
            "signal": str,          # 综合信号：strong / warning / danger / neutral
        }
    """
    levels_detail = {}
    broken = []      # 已失守的均线
    holding = []     # 仍在支撑的均线

    for level in MA_LEVELS:
        key = level["key"]
        ma_val = mas.get(key)
        if ma_val is None:
            levels_detail[key] = {"status": "unknown", "deviation_pct": 0, "label": "数据不足"}
            continue

        status = _single_ma_status(current, ma_val, closes, level["name"])
        deviation = round((current - ma_val) / ma_val * 100, 1)

        # 人类可读标签
        label_map = {
            "above": "站上",
            "near_above": "上方震荡",
            "near_below": "附近争夺",
            "below": "跌破",
            "just_broke_down": "刚跌破",
            "rebound_blocked": "反弹受阻",
        }
        label = label_map.get(status, status)

        levels_detail[key] = {
            "status": status,
            "deviation_pct": deviation,
            "label": label,
            "ma_value": round(ma_val, 2),
        }

        # 分类：失守 vs 支撑
        if status in ("below", "just_broke_down"):
            broken.append(key)
        elif status in ("above", "near_above"):
            holding.append(key)
        # near_below / rebound_blocked 不算完全失守也不算支撑，属于争夺中

    # 防线深度：从最短周期开始数，连续站上的层数 + 处于争夺中的层数
    defense_order = ["MA20", "MA60", "MA120"]
    defense_depth = 0
    for dk in defense_order:
        st = levels_detail.get(dk, {}).get("status", "unknown")
        if st in ("above", "near_above"):
            defense_depth += 1
        elif st in ("near_below", "rebound_blocked"):
            defense_depth += 0.5
        else:
            break  # 失守即停，后面的支撑不计数

    # 信号等级
    if not broken:
        signal = "strong"
    elif len(broken) == 1:
        # MA20 破了但 MA60/MA120 还在 → warning
        # MA60 破了但 MA120 还在 → danger (中期趋势坏了)
        if "MA60" in broken or "MA120" in broken:
            signal = "danger"
        else:
            signal = "warning"
    elif len(broken) >= 2:
        # 两条或以上关键均线失守
        if "MA120" in broken:
            signal = "danger"  # 牛熊线破了
        else:
            signal = "danger" if "MA60" in broken else "warning"
    else:
        signal = "neutral"

    # 摘要
    broken_names = [f"{k}" for k in broken]
    holding_names = [f"{k}" for k in holding]
    if not broken:
        summary = f"全线站上({'+'.join(holding_names)})"
    elif not holding:
        summary = f"全线失守({','.join(broken_names)})"
    else:
        summary = f"失守{','.join(broken_names)}，{'+'.join(holding_names)}支撑"

    return {
        "levels": levels_detail,
        "defense_depth": defense_depth,
        "broken": broken,
        "holding": holding,
        "signal": signal,
        "summary": summary,
    }


def _volume_price_signal(closes: List[float], volumes: List[float]) -> str:
    """
    近5日量价配合信号
    Returns: volume_surge_sell / shrink_stop / down_continuation / volume_surge_rise / shrink_rise / normal
    """
    if len(closes) < 6 or len(volumes) < 6:
        return "normal"

    pct_5d = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] > 0 else 0
    avg_vol_recent = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else 0
    avg_vol_prior = sum(volumes[-6:-3]) / 3 if len(volumes) >= 6 else 1
    vol_ratio = avg_vol_recent / avg_vol_prior if avg_vol_prior > 0 else 1

    if pct_5d < -0.03 and vol_ratio > 1.3:
        return "volume_surge_sell"
    elif pct_5d < -0.03 and vol_ratio < 0.7:
        return "shrink_stop"
    elif pct_5d < -0.01:
        return "down_continuation"
    elif pct_5d > 0.01 and vol_ratio > 1.2:
        return "volume_surge_rise"
    elif pct_5d > 0 and vol_ratio < 0.8:
        return "shrink_rise"
    else:
        return "normal"


# ============================================================
# 单指数分析
# ============================================================

def analyze_single_index(
    name: str,
    kline_records: List[Dict],
    required_days: int = 120,
) -> Dict:
    """
    对单个指数做完整技术位分析（多级均线体系）

    Args:
        name: 指数名称
        kline_records: K 线记录列表
        required_days: 最少需要的数据天数（默认120天以支持MA120）

    Returns:
        技术位分析结果字典
    """
    result = {
        "name": name,
        "status": "unknown",
        "current": None,
        "change_pct": None,
        "ma5": None, "ma10": None,
        "ma20": None, "ma60": None, "ma120": None,
        "alignment": "unknown",
        "ma_levels": None,       # 多级均线分析结果
        "defense_depth": 0,
        "volume_signal": "normal",
        "label": "数据不足",
        "score": 0.5,
    }

    if not kline_records or len(kline_records) < required_days:
        return result

    closes = []
    volumes = []
    for r in kline_records:
        try:
            closes.append(float(r.get("收盘", r.get("close", 0))))
            volumes.append(float(r.get("成交量", r.get("volume", 0))))
        except (ValueError, TypeError):
            continue

    if len(closes) < required_days:
        return result

    current = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else current
    change_pct = (current - prev) / prev * 100 if prev > 0 else 0

    mas = _compute_mas(closes)
    alignment = _ma_alignment(mas)
    ma_levels = _analyze_ma_levels(current, mas, closes)
    vol_signal = _volume_price_signal(closes, volumes)

    score = _calc_index_score(alignment, ma_levels, vol_signal, current, mas)
    label = _build_index_label(alignment, ma_levels, vol_signal)

    result.update({
        "name": name,
        "status": "ok",
        "current": round(current, 2),
        "change_pct": round(change_pct, 2),
        "ma5": round(mas["MA5"], 2) if "MA5" in mas else None,
        "ma10": round(mas["MA10"], 2) if "MA10" in mas else None,
        "ma20": round(mas["MA20"], 2) if "MA20" in mas else None,
        "ma60": round(mas["MA60"], 2) if "MA60" in mas else None,
        "ma120": round(mas["MA120"], 2) if "MA120" in mas else None,
        "alignment": alignment,
        "ma_levels": ma_levels,
        "defense_depth": ma_levels.get("defense_depth", 0),
        "volume_signal": vol_signal,
        "label": label,
        "score": score,
    })
    return result


def _calc_index_score(
    alignment: str,
    ma_levels: Dict,
    vol_signal: str,
    current: float,
    mas: Dict[str, float],
) -> float:
    """
    单指数评分 0.0-1.0（基于多级均线体系）

    权重分配：
    - 均线排列（MA5/10/20）：±0.20
    - MA20 攻防：±0.10
    - MA60 攻防：±0.20  （权重最高，中期趋势核心）
    - MA120 攻防：±0.15
    - 量价信号：±0.15
    """
    score = 0.5

    # 均线排列
    if alignment == "bullish":
        score += 0.20
    elif alignment == "bearish":
        score -= 0.20
    elif alignment == "converging":
        pass  # 粘合不加减

    # 逐层加减分
    if ma_levels:
        levels = ma_levels.get("levels", {})
        level_weights = {
            "MA20": 0.10,
            "MA60": 0.20,
            "MA120": 0.15,
        }
        for key, weight in level_weights.items():
            lv = levels.get(key, {})
            status = lv.get("status", "unknown")
            if status in ("above",):
                score += weight
            elif status in ("near_above",):
                score += weight * 0.5
            elif status in ("below", "just_broke_down"):
                score -= weight
            elif status in ("near_below", "rebound_blocked"):
                score -= weight * 0.5

    # 量价
    vol_score_map = {
        "volume_surge_rise": +0.15,
        "shrink_stop": +0.10,
        "volume_surge_sell": -0.15,
        "shrink_rise": -0.05,
        "down_continuation": -0.10,
    }
    score += vol_score_map.get(vol_signal, 0)

    # MA20 上方小幅加成
    ma20 = mas.get("MA20")
    if ma20 and current > ma20:
        score += 0.05

    return max(0.0, min(1.0, score))


def _build_index_label(
    alignment: str,
    ma_levels: Dict,
    vol_signal: str,
) -> str:
    """构建单指数的可读标签（多级均线摘要）"""
    parts = []

    # 均线排列
    if alignment == "bullish":
        parts.append("多头排列")
    elif alignment == "bearish":
        parts.append("空头排列")
    elif alignment == "converging":
        parts.append("均线粘合")

    # 多级均线摘要
    if ma_levels:
        summary = ma_levels.get("summary", "")
        if summary:
            parts.append(summary)

    # 量价
    vol_map = {
        "volume_surge_sell": "放量下杀",
        "shrink_stop": "缩量止跌",
        "down_continuation": "阴跌中继",
        "volume_surge_rise": "放量反弹",
        "shrink_rise": "缩量弱反弹",
    }
    vol_text = vol_map.get(vol_signal, "")
    if vol_text:
        parts.append(vol_text)

    return "/".join(parts) if parts else "无明显信号"


# ============================================================
# 双创综合分析
# ============================================================

def analyze_gem_sci_tech(
    gem_kline: List[Dict] = None,
    star_kline: List[Dict] = None,
) -> Dict:
    """
    对双创指数做综合分析，产出综合标签

    Args:
        gem_kline: 创业板指 K 线
        star_kline: 科创50 K 线

    Returns:
        {
            "gem": {...},
            "star": {...},
            "composite_label": str,
            "composite_score": float,
            "trend_judgment": str,
            "risk_flag": str,       # none / warning / danger
        }
    """
    result = {
        "gem": None,
        "star": None,
        "composite_label": "数据不足",
        "composite_score": 0.5,
        "trend_judgment": "震荡分化",
        "risk_flag": "none",
    }

    try:
        import akshare as ak

        if gem_kline is None:
            try:
                df_gem = ak.stock_zh_index_daily(symbol="sz399006")
                if df_gem is not None and len(df_gem) >= 120:
                    gem_kline = df_gem.to_dict("records")
            except Exception as e:
                logger.warning("创业板指 K 线获取失败: %s", e)

        if star_kline is None:
            try:
                df_star = ak.stock_zh_index_daily(symbol="sz399688")
                if df_star is not None and len(df_star) >= 120:
                    star_kline = df_star.to_dict("records")
            except Exception as e:
                logger.warning("科创50 K 线获取失败: %s", e)

    except ImportError:
        logger.warning("AKShare 不可用，无法获取双创 K 线")
    except Exception as e:
        logger.warning("双创 K 线获取异常: %s", e)

    gem_result = analyze_single_index("创业板指", gem_kline or [])
    star_result = analyze_single_index("科创50", star_kline or [])
    result["gem"] = gem_result
    result["star"] = star_result

    valid_results = [r for r in [gem_result, star_result] if r.get("status") == "ok"]
    if not valid_results:
        return result

    scores = [r["score"] for r in valid_results]
    composite_score = sum(scores) / len(scores)
    result["composite_score"] = round(composite_score, 2)

    labels = [r["label"] for r in valid_results]
    result["composite_label"] = " | ".join(labels)

    # 趋势判断（结合多级均线信号）
    gem_score = gem_result.get("score", 0.5) if gem_result else 0.5
    star_score = star_result.get("score", 0.5) if star_result else 0.5
    gem_signal = (gem_result.get("ma_levels") or {}).get("signal", "neutral") if gem_result else "neutral"
    star_signal = (star_result.get("ma_levels") or {}).get("signal", "neutral") if star_result else "neutral"

    # danger 信号计数
    danger_count = sum(1 for s in (gem_signal, star_signal) if s == "danger")
    warning_count = sum(1 for s in (gem_signal, star_signal) if s == "warning")

    if gem_score >= 0.7 and star_score >= 0.7:
        result["trend_judgment"] = "强势共振"
        result["risk_flag"] = "none"
    elif gem_score <= 0.3 and star_score <= 0.3:
        gem_vol = gem_result.get("volume_signal", "") if gem_result else ""
        star_vol = star_result.get("volume_signal", "") if star_result else ""
        if "shrink_stop" in (gem_vol, star_vol):
            result["trend_judgment"] = "止跌企稳"
            result["risk_flag"] = "warning"
        else:
            result["trend_judgment"] = "下跌中继"
            result["risk_flag"] = "danger" if danger_count >= 1 else "warning"
    elif danger_count >= 2:
        result["trend_judgment"] = "下跌中继"
        result["risk_flag"] = "danger"
    elif abs(gem_score - star_score) > 0.3:
        result["trend_judgment"] = "分化"
        result["risk_flag"] = "warning" if danger_count >= 1 or gem_score < 0.4 or star_score < 0.4 else "none"
    else:
        result["trend_judgment"] = "震荡分化"
        result["risk_flag"] = "warning" if warning_count >= 1 else "none"

    logger.info(
        "双创技术位: 创业板=%s(%.2f,sig=%s) 科创50=%s(%.2f,sig=%s) → %s (risk=%s,danger=%d)",
        gem_result.get("label", "N/A") if gem_result else "N/A",
        gem_score, gem_signal,
        star_result.get("label", "N/A") if star_result else "N/A",
        star_score, star_signal,
        result["trend_judgment"], result["risk_flag"], danger_count,
    )

    return result


def gem_sci_tech_to_mode_score(trend_judgment: str) -> float:
    """
    将双创趋势判断转换为 market_mode_adaptive 的维度分 (0.0-1.0)
    """
    mapping = {
        "强势共振": 1.0,
        "震荡分化": 0.5,
        "止跌企稳": 0.3,
        "分化": 0.4,
        "下跌中继": 0.0,
    }
    return mapping.get(trend_judgment, 0.5)


# ============================================================
# 单例缓存
# ============================================================

_instance: Optional[Dict] = None
_instance_date: Optional[str] = None


def get_gem_sci_tech_analysis(force_refresh: bool = False) -> Dict:
    """获取双创技术位分析（当日缓存）"""
    global _instance, _instance_date
    today = datetime.now().strftime("%Y-%m-%d")
    if not force_refresh and _instance_date == today and _instance is not None:
        return _instance
    _instance = analyze_gem_sci_tech()
    _instance_date = today
    return _instance
