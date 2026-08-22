"""
外围市场扰动分析器
获取美股隔夜行情 + VIX + 美股期货 → 产出扰动等级 → 调节市场模式

纯数据驱动，不消耗问财配额。
"""
import logging
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# 扰动等级判定
# ============================================================

def assess_disturbance(
    us_snapshot: Dict = None,
    us_futures: Dict = None,
    vix: float = 0.0,
    sp500_change: float = 0.0,
    nasdaq_change: float = 0.0,
    sp500_futures_change: float = 0.0,
) -> Dict:
    """
    评估外围市场对 A 股的扰动等级

    规则：
    - VIX > 30 或 美股跌超 2% → 严重扰动
    - VIX 25-30 或 美股跌 1-2% → 中度扰动
    - VIX 20-25 或 美股微跌 → 轻度扰动
    - 其他 → 无影响

    Args:
        us_snapshot: AKShare adapter 返回的美股快照 (可选)
        us_futures: AKShare adapter 返回的期货快照 (可选)
        vix: VIX 值（如果直接传入）
        sp500_change: 标普500 涨跌幅
        nasdaq_change: 纳斯达克 涨跌幅
        sp500_futures_change: 标普500 期货涨跌幅

    Returns:
        {
            "level": "无影响" | "轻度扰动" | "中度扰动" | "严重扰动",
            "level_code": 0 | 1 | 2 | 3,
            "vix": float,
            "sp500_change": float,
            "nasdaq_change": float,
            "sp500_futures_change": float,
            "reasons": [str],    # 触发原因
            "summary": str,      # 一句话摘要
        }
    """
    # 合并数据源（字段为 None = 无数据，跳过对应判定；P0-1 审计 2026-08-18）
    if us_snapshot:
        vix = us_snapshot.get("vix", vix)
        sp500_change = us_snapshot.get("sp500_change_pct", sp500_change)
        nasdaq_change = us_snapshot.get("nasdaq_change_pct", nasdaq_change)
    if us_futures:
        sp500_futures_change = us_futures.get("sp500_futures_change_pct", sp500_futures_change)

    reasons = []
    severity = 0  # 0=None, 1=Mild, 2=Moderate, 3=Severe

    # VIX 判定（None = 无数据，跳过；不再用 0 当脏值兜底）
    if vix is not None and vix >= 30:
        severity = max(severity, 3)
        reasons.append(f"VIX={vix:.1f}（恐慌区间>30）")
    elif vix is not None and vix >= 25:
        severity = max(severity, 2)
        reasons.append(f"VIX={vix:.1f}（中度不安 25-30）")
    elif vix is not None and vix >= 20:
        severity = max(severity, 1)
        reasons.append(f"VIX={vix:.1f}（轻度不安 20-25）")

    # 美股跌幅判定（百分比值，与 AKShare 返回一致：-1.5 表示 -1.5%）
    # 使用标普500为主，纳斯达克为辅
    us_decline = sp500_change  # 默认用标普500
    # 只有当纳斯达克数据存在且两者都跌时，取跌幅更大的那个
    if nasdaq_change is not None and sp500_change is not None:
        if nasdaq_change < sp500_change:
            us_decline = nasdaq_change
        else:
            us_decline = sp500_change

    if us_decline is not None and us_decline < -2.0:
        severity = max(severity, 3)
        reasons.append(f"美股跌{abs(us_decline):.1f}%（>2%）")
    elif us_decline is not None and us_decline < -1.0:
        severity = max(severity, 2)
        reasons.append(f"美股跌{abs(us_decline):.1f}%（1-2%）")
    elif us_decline is not None and us_decline < 0:
        severity = max(severity, 1)
        reasons.append(f"美股微跌{abs(us_decline):.1f}%")

    # 期货判定（盘前参考，百分比值）
    if sp500_futures_change is not None and sp500_futures_change < -1.0:
        severity = max(severity, 2)
        reasons.append(f"期货跌{abs(sp500_futures_change):.1f}%")
    elif sp500_futures_change is not None and sp500_futures_change < -0.5:
        severity = max(severity, 1)
        reasons.append(f"期货微跌{abs(sp500_futures_change):.1f}%")

    # 映射等级
    level_map = {0: "无影响", 1: "轻度扰动", 2: "中度扰动", 3: "严重扰动"}
    level = level_map.get(severity, "无影响")

    def _r(v):
        return round(v, 2) if v is not None else None

    # 构建摘要
    parts = []
    if sp500_change not in (None, 0):
        parts.append(f"隔夜美股{sp500_change:+.1f}%")
    if vix is not None and vix > 0:
        parts.append(f"VIX {vix:.1f}")
    if sp500_futures_change not in (None, 0):
        parts.append(f"期货{sp500_futures_change:+.1f}%")
    summary = " / ".join(parts) + (f" → {level}" if parts else "无外围数据")

    return {
        "level": level,
        "level_code": severity,
        "vix": _r(vix),
        "sp500_change": _r(sp500_change),
        "nasdaq_change": _r(nasdaq_change),
        "sp500_futures_change": _r(sp500_futures_change),
        "reasons": reasons,
        "summary": summary,
    }


def apply_external_downgrade(
    current_mode: str,
    disturbance_level_code: int,
) -> str:
    """
    根据外围扰动等级对市场模式做降级

    Args:
        current_mode: 当前模式 "attack" / "defend" / "retreat"
        disturbance_level_code: 扰动等级码 0-3

    Returns:
        降级后的模式
    """
    if disturbance_level_code < 2:
        return current_mode  # 轻度或无影响，不降级

    mode_order = {"attack": 3, "defend": 2, "retreat": 1}

    if disturbance_level_code == 3:  # 严重扰动
        # 强制降为 retreat（最低）
        if current_mode == "attack":
            logger.warning("外围严重扰动，模式降级: attack → defend（外围因子）")
            return "defend"
        elif current_mode == "defend":
            logger.warning("外围严重扰动，模式降级: defend → retreat（外围因子）")
            return "retreat"
        return "retreat"
    elif disturbance_level_code == 2:  # 中度扰动
        # 降一级
        if current_mode == "attack":
            logger.info("外围中度扰动，模式降级: attack → defend")
            return "defend"
        return current_mode

    return current_mode


# ============================================================
# 便捷入口：一站式拉取 + 评估
# ============================================================

_instance_cache: Optional[Dict] = None
_instance_date: Optional[str] = None


def get_external_market_assessment(force_refresh: bool = False) -> Dict:
    """
    一站式获取外围市场评估（当日缓存）

    Returns:
        {
            "disturbance": {...},     # assess_disturbance 的结果
            "us_snapshot": {...},     # 美股快照原始数据
            "us_futures": {...},      # 期货快照原始数据
            "mode_downgrade_applied": bool,  # 是否触发了降级
        }
    """
    global _instance_cache, _instance_date
    today = datetime.now().strftime("%Y-%m-%d")
    if not force_refresh and _instance_date == today and _instance_cache is not None:
        return _instance_cache

    result = {
        "disturbance": None,
        "us_snapshot": None,
        "us_futures": None,
        "mode_downgrade_applied": False,
    }

    us_snapshot = None
    us_futures = None

    try:
        from ..data_layer.akshare_adapter import get_akshare_adapter
        adapter = get_akshare_adapter()

        # 获取美股快照
        snap_result = adapter.get_us_market_snapshot()
        if snap_result.success and snap_result.data:
            us_snapshot = snap_result.data
            result["us_snapshot"] = us_snapshot

        # 获取美股期货
        fut_result = adapter.get_us_futures_snapshot()
        if fut_result.success and fut_result.data:
            us_futures = fut_result.data
            result["us_futures"] = us_futures

    except ImportError:
        logger.warning("AKShare 不可用，无法获取外围数据")
    except Exception as e:
        logger.warning("获取外围数据失败: %s", e)

    # 评估扰动等级
    disturbance = assess_disturbance(
        us_snapshot=us_snapshot,
        us_futures=us_futures,
    )
    result["disturbance"] = disturbance

    _instance_cache = result
    _instance_date = today
    return result
