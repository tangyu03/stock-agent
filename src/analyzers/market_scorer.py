"""
大盘评分引擎 - 第一层决策
六维加权评分 → 0-10分 → 操作模式（进攻/防守/撤退）

修复#4：采用加权评分替代均等评分 + 趋势加速度惩罚
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, date
from dataclasses import dataclass, field

from ..config_models import load_config
from ..db import get_connection
from ..data_layer.akshare_adapter import get_akshare_adapter
from ..data_layer.skill_wrapper import get_skill_wrapper
from ..data_layer.data_cache import get_data_cache

logger = logging.getLogger(__name__)


@dataclass
class DimensionScore:
    """单个维度评分结果"""
    name: str
    key: str
    raw_score: float       # 原始分 0-10
    weight: float          # 权重 0-1
    weighted_score: float  # 加权分 = raw_score * weight
    condition: str         # 命中的条件描述
    data_source: str = ""  # 数据来源


@dataclass
class MarketScoreResult:
    """大盘评分完整结果"""
    date: str
    dimensions: List[DimensionScore] = field(default_factory=list)
    total_weighted_score: float = 0.0  # 加权总分 0-10
    momentum_penalty: float = 0.0      # 趋势加速度惩罚
    final_score: float = 0.0           # 最终分数 = total - penalty
    mode: str = "defend"               # attack / defend / retreat
    position_limit: float = 0.5        # 对应仓位上限
    score_history_5d: List[float] = field(default_factory=list)  # 近5日评分
    details: Dict[str, Any] = field(default_factory=dict)


class MarketScorer:
    """大盘评分引擎"""

    def __init__(self):
        self._config = load_config("market_scoring.yaml")
        self._scoring_cfg = self._config["scoring"]
        # P0-3: momentum 在 scoring 下，mode_mapping 在 voting 下
        self._momentum_cfg = self._config.get("scoring", {}).get("momentum", {})
        self._mode_mapping = self._config.get("voting", {}).get("mode_mapping", {})
        self._akshare = get_akshare_adapter()
        self._skill = get_skill_wrapper()
        self._cache = get_data_cache()

    def score(self, target_date: Optional[str] = None) -> MarketScoreResult:
        """
        执行大盘评分

        ⚠️ 架构定位（P2-6 澄清）：本方法是六维实时评分器，当前**不在实盘主路径上**。
        实盘主路径用 market_mode_adaptive.assess_daily（5 维，历史指数K线+简化规则）；
        orchestrator 仅在 assess_daily 失败时回退到本类的 get_current_mode()（读缓存/默认值）。
        因此 score() 的六维实时计算在实盘生产中是"保留实现"，改动前请先确认调用方。

        Args:
            target_date: 目标日期 YYYY-MM-DD，默认今天

        Returns:
            MarketScoreResult 完整评分结果
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=== 大盘评分开始 %s ===", target_date)

        result = MarketScoreResult(date=target_date)

        # 1. 六维评分
        weights = self._scoring_cfg.get("weights", {})
        dimensions_cfg = self._scoring_cfg.get("dimensions", {})

        for dim_key, dim_cfg in dimensions_cfg.items():
            weight = weights.get(dim_key, 0.1)
            dim_score = self._score_dimension(dim_key, dim_cfg, target_date)
            dim_score.weight = weight
            dim_score.weighted_score = dim_score.raw_score * weight
            result.dimensions.append(dim_score)

        # 2. 计算加权总分
        result.total_weighted_score = sum(d.weighted_score for d in result.dimensions)

        # 3. 趋势加速度惩罚
        result.score_history_5d = self._get_score_history(5)
        result.momentum_penalty = self._calculate_momentum_penalty(result.score_history_5d)

        # 4. 最终分数
        result.final_score = max(0, result.total_weighted_score - result.momentum_penalty)

        # 5. 映射操作模式
        mode_info = self._map_to_mode(result.final_score)
        result.mode = mode_info["mode"]
        result.position_limit = mode_info["position_limit"]

        # 6. 保存评分结果
        self._save_score(result)

        logger.info(
            "大盘评分完成: 加权总分=%.2f, 惯性惩罚=%.2f, 最终=%.2f → %s (仓位上限%.0f%%)",
            result.total_weighted_score,
            result.momentum_penalty,
            result.final_score,
            result.mode,
            result.position_limit * 100,
        )

        return result

    def _score_dimension(self, dim_key: str, dim_cfg: Dict, target_date: str) -> DimensionScore:
        """
        对单个维度进行评分

        Args:
            dim_key: 维度键名
            dim_cfg: 维度配置
            target_date: 目标日期

        Returns:
            DimensionScore
        """
        dim_name = dim_cfg.get("name", dim_key)
        rules = dim_cfg.get("rules", [])

        # 获取该维度的数据
        dim_data = self._fetch_dimension_data(dim_key, target_date)

        # 根据规则评分
        score = 0.0
        hit_condition = "未命中任何规则"

        for rule in rules:
            condition = rule["condition"]
            if self._evaluate_condition(dim_key, condition, dim_data):
                score = rule["score"]
                hit_condition = rule.get("desc", condition)
                break

        return DimensionScore(
            name=dim_name,
            key=dim_key,
            raw_score=score,
            weight=0,  # 由外部设置
            weighted_score=0,
            condition=hit_condition,
        )

    def _fetch_dimension_data(self, dim_key: str, target_date: str) -> Dict[str, Any]:
        """
        获取维度所需的市场数据

        优先AKShare主源，不再使用问财降级（优化 #5）。
        AKShare不可用时维度评分为 0，由评分规则兜底。
        """
        data = {}

        if dim_key == "index_trend":
            result = self._akshare.get_index_data("000001")
            if result.success and result.data:
                data["index_data"] = result.data
                data["ma20_position"] = self._calculate_ma_position(result.data, period=20)

        elif dim_key == "ma_alignment":
            result = self._akshare.get_index_data("000001")
            if result.success and result.data:
                data["ma_alignment"] = self._calculate_ma_alignment(result.data)

        elif dim_key == "volume":
            result = self._akshare.get_market_volume()
            if result.success and result.data:
                data["total_volume_yi"] = result.data.get("total_volume_yi", 0)

        elif dim_key == "advance_decline":
            result = self._akshare.get_advance_decline()
            if result.success and result.data:
                data["advance_decline_ratio"] = result.data.get("advance_decline_ratio", 0)

        elif dim_key == "limit_stats":
            date_str = target_date.replace("-", "")
            zt_result = self._akshare.get_zt_pool(date_str)
            dt_result = self._akshare.get_dt_pool(date_str)
            data["zt_count"] = len(zt_result.data) if zt_result.success and zt_result.data else 0
            data["dt_count"] = len(dt_result.data) if dt_result.success and dt_result.data else 0

        elif dim_key == "sentiment":
            # 情绪辅助 - 优先问财
            try:
                result = self._skill.query_market_sentiment()
                if result and result.get("status") == "ok" and result.get("data"):
                    raw_data = result.get("data", {})
                    # 问财API可能返回列表（多条记录）
                    if isinstance(raw_data, list) and len(raw_data) > 0:
                        # 问财返回的可能是带贝塔系数的股票列表，需要从中推断市场情绪
                        # 用所有股票的平均涨跌幅来推断情绪
                        changes = []
                        for record in raw_data:
                            if isinstance(record, dict):
                                chg = self._safe_float(record, ["最新涨跌幅", "最新涨跌幅:前复权", "涨跌幅"])
                                if chg is not None:
                                    changes.append(chg)
                        if changes:
                            avg_change = sum(changes) / len(changes)
                            # 映射涨跌幅到恐慌贪婪指数：0-100
                            fgi = max(0, min(100, 50 + avg_change * 10))
                            data["sentiment_data"] = {"fear_greed_index": round(fgi, 1)}
                        else:
                            data["sentiment_data"] = {"fear_greed_index": 50}
                    elif isinstance(raw_data, dict):
                        data["sentiment_data"] = raw_data
                    else:
                        data["sentiment_data"] = {"fear_greed_index": 50}
                    logger.info("情绪数据: fear_greed_index=%s", data["sentiment_data"].get("fear_greed_index"))
                else:
                    data["sentiment_data"] = {"fear_greed_index": 50}  # 默认中性
            except Exception as e:
                logger.warning("获取市场情绪数据失败: %s，使用默认值", e)
                data["sentiment_data"] = {"fear_greed_index": 50}

        return data

    @staticmethod
    def _safe_float(data: Dict, keys: list, default=None) -> Optional[float]:
        """从字典中按多个可能的字段名安全提取浮点值"""
        for key in keys:
            val = data.get(key)
            if val is not None:
                try:
                    if isinstance(val, str):
                        val = val.replace("%", "").replace(",", "").strip()
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return default

    def _extract_fear_greed_index(self, record: Dict) -> float:
        """
        从问财API返回的记录中提取恐慌贪婪指数
        
        问财返回的字段名不确定，尝试多种可能
        """
        # 尝试多种可能的字段名
        fgi_keys = [
            "恐慌贪婪指数", "恐惧贪婪指数", "fear_greed_index", "贪婪指数",
            "市场情绪指数", "情绪指数", "信心指数",
        ]
        for key in fgi_keys:
            val = record.get(key)
            if val is not None:
                try:
                    if isinstance(val, str):
                        val = val.replace("%", "").replace(",", "").strip()
                    return float(val)
                except (ValueError, TypeError):
                    continue
        
        # 如果没有直接的恐慌贪婪指数字段，尝试从涨跌幅等间接推断
        # 如果有涨跌幅字段，可以用涨跌幅推算一个简单的情绪值
        change_keys = ["涨跌幅", "change_pct", "涨跌"]
        for key in change_keys:
            val = record.get(key)
            if val is not None:
                try:
                    change = float(str(val).replace("%", "").replace(",", "").strip())
                    # 简单映射：涨跌幅 -> 情绪值 (0-100)
                    # +5% -> 95, 0% -> 50, -5% -> 5
                    fgi = 50 + change * 10
                    return max(0, min(100, fgi))
                except (ValueError, TypeError):
                    continue
        
        # 默认中性
        return 50

    def _evaluate_condition(self, dim_key: str, condition: str, data: Dict) -> bool:
        """
        评估条件是否满足

        Args:
            dim_key: 维度键名
            condition: 条件字符串
            data: 维度数据

        Returns:
            条件是否满足
        """
        if dim_key == "index_trend":
            ma_pos = data.get("ma20_position", "unknown")
            if condition == "above_ma20":
                return ma_pos == "above"
            elif condition == "near_ma20":
                return ma_pos == "near"
            elif condition == "below_ma20":
                return ma_pos == "below"

        elif dim_key == "ma_alignment":
            alignment = data.get("ma_alignment", "unknown")
            if condition == "bullish":
                return alignment == "bullish"
            elif condition == "converging":
                return alignment == "converging"
            elif condition == "bearish":
                return alignment == "bearish"

        elif dim_key == "volume":
            vol = data.get("total_volume_yi", 0)
            if condition == "above_1t":
                return vol > 10000  # 1万亿 = 10000亿
            elif condition == "between_8k_1t":
                return 8000 <= vol <= 10000
            elif condition == "below_8k":
                return vol < 8000

        elif dim_key == "advance_decline":
            ratio = data.get("advance_decline_ratio", 0)
            if condition == "ratio_above_2":
                return ratio > 2
            elif condition == "ratio_between":
                return 0.8 <= ratio <= 2
            elif condition == "ratio_below_08":
                return ratio < 0.8

        elif dim_key == "limit_stats":
            zt = data.get("zt_count", 0)
            dt = data.get("dt_count", 99)
            if condition == "strong":
                return zt > 50 and dt < 10
            elif condition == "moderate":
                return 30 <= zt <= 50
            elif condition == "weak":
                return zt < 30

        elif dim_key == "sentiment":
            sentiment = data.get("sentiment_data", {})
            fgi = sentiment.get("fear_greed_index", 50)
            if condition == "greedy":
                return fgi > 70
            elif condition == "neutral":
                return 30 <= fgi <= 70
            elif condition == "fearful":
                return fgi < 30

        return False

    def _calculate_ma_position(self, kline_data: list, period: int = 20) -> str:
        """
        计算指数相对于MA的位置

        Returns:
            "above" / "near" / "below"
        """
        if not kline_data or len(kline_data) < period:
            return "unknown"

        # 取最近period天的收盘价
        recent = kline_data[-period:] if isinstance(kline_data, list) else []
        if not recent:
            return "unknown"

        try:
            closes = [float(k.get("收盘", k.get("close", 0))) for k in recent]
            if len(closes) < period:
                return "unknown"

            ma_value = sum(closes) / len(closes)
            current = closes[-1]
            deviation = (current - ma_value) / ma_value

            if deviation > 0.02:
                return "above"
            elif deviation > -0.02:
                return "near"
            else:
                return "below"
        except (KeyError, ValueError, TypeError):
            return "unknown"

    def _calculate_ma_alignment(self, kline_data: list) -> str:
        """
        计算均线排列状态

        Returns:
            "bullish" (MA5>MA10>MA20) / "converging" / "bearish"
        """
        if not kline_data or len(kline_data) < 20:
            return "unknown"

        try:
            closes = [float(k.get("收盘", k.get("close", 0))) for k in kline_data]

            if len(closes) < 20:
                return "unknown"

            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20

            # 均线间距
            max_ma = max(ma5, ma10, ma20)
            min_ma = min(ma5, ma10, ma20)
            spread = (max_ma - min_ma) / max_ma

            if ma5 > ma10 > ma20:
                return "bullish"
            elif spread < 0.01:  # 间距<1%
                return "converging"
            elif ma5 < ma10 < ma20:
                return "bearish"
            else:
                return "converging"
        except (KeyError, ValueError, TypeError):
            return "unknown"

    def _calculate_momentum_penalty(self, score_history: List[float]) -> float:
        """
        计算趋势加速度惩罚

        如果评分连续下降，即使当前分数尚可也倾向保守解读

        Args:
            score_history: 近N日评分历史

        Returns:
            惩罚分数
        """
        if len(score_history) < 2:
            return 0.0

        consecutive_decline = 0
        for i in range(len(score_history) - 1, 0, -1):
            if score_history[i] < score_history[i - 1]:
                consecutive_decline += 1
            else:
                break

        if consecutive_decline < self._momentum_cfg.get("consecutive_decline_days", 3):
            return 0.0

        penalty_per_day = self._momentum_cfg.get("penalty_per_day", 0.5)
        max_penalty = self._momentum_cfg.get("max_penalty", 2.0)

        penalty = (consecutive_decline - self._momentum_cfg.get("consecutive_decline_days", 3) + 1) * penalty_per_day
        return min(penalty, max_penalty)

    def _get_score_history(self, days: int) -> List[float]:
        """获取近N日评分历史"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT score FROM market_score_history ORDER BY date DESC LIMIT ?",
                (days,),
            )
            rows = cursor.fetchall()
            return [row["score"] for row in reversed(rows)]
        except Exception as e:
            logger.error("Failed to get score history: %s", e)
            return []
        finally:
            conn.close()

    def _map_to_mode(self, score: float) -> Dict[str, Any]:
        """将评分映射到操作模式"""
        for mode_name, mode_cfg in self._mode_mapping.items():
            if mode_cfg["min"] <= score <= mode_cfg["max"]:
                return {
                    "mode": mode_name,
                    "position_limit": mode_cfg["position_limit"],
                }
        # 默认防守
        return {"mode": "defend", "position_limit": 0.5}

    def _save_score(self, result: MarketScoreResult):
        """保存评分结果到数据库"""
        conn = get_connection()
        try:
            import json
            details = {
                "dimensions": [
                    {
                        "name": d.name,
                        "key": d.key,
                        "raw_score": d.raw_score,
                        "weight": d.weight,
                        "weighted_score": d.weighted_score,
                        "condition": d.condition,
                    }
                    for d in result.dimensions
                ],
                "total_weighted_score": result.total_weighted_score,
                "momentum_penalty": result.momentum_penalty,
                "score_history_5d": result.score_history_5d,
            }

            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO market_score_history (date, score, mode, details)
                VALUES (?, ?, ?, ?)
                """,
                (result.date, result.final_score, result.mode, json.dumps(details, ensure_ascii=False)),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to save market score: %s", e)
        finally:
            conn.close()

    def get_current_mode(self) -> Dict[str, Any]:
        """
        获取当前操作模式（从最近一次评分记录中读取）

        Returns:
            {"mode": "attack/defend/retreat", "score": 7.5, "position_limit": 0.8}
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT score, mode FROM market_score_history ORDER BY date DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                mode_cfg = self._mode_mapping.get(row["mode"], {})
                return {
                    "mode": row["mode"],
                    "score": row["score"],
                    "position_limit": mode_cfg.get("position_limit", 0.5),
                }
        except Exception as e:
            logger.error("Failed to get current mode: %s", e)
        finally:
            conn.close()

        return {"mode": "defend", "score": 5.0, "position_limit": 0.5}


# 单例
_instance: Optional[MarketScorer] = None


def get_market_scorer() -> MarketScorer:
    global _instance
    if _instance is None:
        _instance = MarketScorer()
    return _instance
