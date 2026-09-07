"""
B3 虹吸度（bankuai.md v2，S系列阶段B）

  suction_level = 板块成交额 / 分母
  suction_state：
    5日变化率 >  rising  (+30%) → 'siphoning'（冻结对立面进场）
    5日变化率 <  falling (-20%) → 'releasing'（D1 置信+1）
    其余                           → 'stable'

口径坑（登记于 bankuai.md）：
  行业层分母 = 全市场成交额（行业一览真占比）
  概念层分母 = 仅跟踪子集成交额之和（**子集相对强度，非市场真占比**）
  两套虹吸不同量纲，不可直接比较；概念虹吸仅周报展示与 C1b 近似覆盖，不进入消费。

本模块只算板块自身的 5 日变化率状态；分母构造由调用方（B5）负责。
"""
import logging
from typing import Dict, Optional, Sequence

logger = logging.getLogger(__name__)


def _suction_cfg() -> Dict:
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        return cfg.get("suction", {})
    except Exception:
        return {}


def state_from_series(ratio_series: Sequence[float],
                      rising: float = 0.30, falling: float = -0.20,
                      lookback: int = 5) -> Optional[Dict]:
    """给定逐日 suction_level 序列（升序），判最新状态。

    Returns:
        {suction_level, change_5d, suction_state}；序列不足 lookback+1 返回 None。
    """
    s = list(ratio_series)
    if len(s) < lookback + 1:
        return None
    level = float(s[-1])
    base = float(s[-1 - lookback])
    if base <= 0:
        return None
    change = level / base - 1.0
    if change > rising:
        state = "siphoning"
    elif change < falling:
        state = "releasing"
    else:
        state = "stable"
    return {"suction_level": round(level, 4), "change_5d": round(change, 4),
            "suction_state": state}


def industry_level_series(board_amounts: Dict[str, Sequence[float]],
                          market_amounts: Sequence[float],
                          board: str) -> Sequence[float]:
    """行业层 suction_level 序列 = 行业成交额 / 全市场成交额（逐日）。

    Args:
        board_amounts: {行业名: 逐日成交额（对齐 trade_date 升序）}
        market_amounts: 全市场逐日成交额（同对齐）
    """
    ba = list(board_amounts.get(board, []))
    if not ba or len(ba) != len(list(market_amounts)):
        return []
    m = list(market_amounts)
    return [ba[i] / m[i] if m[i] > 0 else 0.0 for i in range(len(ba))]


def concept_level_series(concept_amounts: Dict[str, Sequence[float]],
                         concept: str) -> Sequence[float]:
    """概念层 suction_level 序列 = 概念成交额 / 跟踪子集成交额之和（子集相对强度）。"""
    names = list(concept_amounts.keys())
    total = [0.0] * (len(list(concept_amounts.values())[0]) if concept_amounts else 0)
    for nm in names:
        vals = list(concept_amounts[nm])
        for i, v in enumerate(vals):
            if i < len(total):
                total[i] += v
    ca = list(concept_amounts.get(concept, []))
    if len(ca) != len(total):
        return []
    return [ca[i] / total[i] if total[i] > 0 else 0.0 for i in range(len(ca))]
