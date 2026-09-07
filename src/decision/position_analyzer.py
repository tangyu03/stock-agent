"""
持仓分析器

【新增模块 — P0 修复】
aggregator.py 引用 self._position_analyzer 但从未初始化，导致 AttributeError。
本模块提供 PositionAnalyzer 的最小可运行实现：

- prefetch_quotes(): 委托 stock_filter 批量预取行情
- analyze_all_holdings(): 基于 timing_engine.check_exit_signals 产出
  HoldingHealth 列表，无需依赖外部问财 API（原工程的问财配额已耗尽）

设计原则：
1. 不引入新依赖，仅复用 timing_engine + stock_filter
2. 出场信号由 timing_engine 统一负责（与 unified_engine 保持一致）
3. 健康度 rating 由出场信号紧急度推导：无信号=健康，预警=观察，止损=警告，紧急=危险
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Any

from .holding_health import HoldingHealth

logger = logging.getLogger(__name__)


class PositionAnalyzer:
    """持仓分析器（最小可运行实现）"""

    def __init__(self):
        # _skill 占位：原工程依赖问财 API，配额耗尽时 _api_cooldown_until > 0
        # 修复后 PositionAnalyzer 不再依赖问财，但保留 _skill 属性以兼容
        # aggregator.run_daily_analysis 中的配额检查代码
        self._skill = _SkillCooldownStub()

    def prefetch_quotes(self, stock_codes: List[str]) -> None:
        """批量预取行情（委托 stock_filter 共享缓存）"""
        if not stock_codes:
            return
        try:
            from ..analyzers.stock_filter import get_stock_filter
            get_stock_filter().prefetch_quotes(stock_codes)
        except Exception as e:
            logger.warning("持仓分析器预取行情失败: %s", e)

    def analyze_all_holdings(
        self,
        holdings: List[Dict],
        market_mode: str,
        sector_classifications: Optional[Dict[str, str]] = None,
    ) -> List[HoldingHealth]:
        """
        分析所有持仓的健康度

        Args:
            holdings: 持仓列表 [{"code": "xxx", "name": "xxx", ...}, ...]
            market_mode: 当前操作模式 attack/defend/retreat
            sector_classifications: 股票代码 → 板块状态映射（可选）

        Returns:
            List[HoldingHealth]，每个持仓一个
        """
        if not holdings:
            return []

        from ..analyzers.timing_engine import get_timing_engine
        te = get_timing_engine()

        # 模式驱动的建议动作
        mode_adj_map = {
            "attack": "可持有/加仓",
            "defend": "减半仓观察",
            "retreat": "清仓避险",
        }
        mode_adjustment = mode_adj_map.get(market_mode, "")

        results: List[HoldingHealth] = []
        for h in holdings:
            if not isinstance(h, dict):
                continue
            code = h.get("code", "")
            name = h.get("name", code)
            if not code:
                continue

            sector_status = (sector_classifications or {}).get(code, "unknown")
            sector_name = h.get("sector", "")

            # 调用 timing_engine 出场检查
            exit_signals = []
            tech_data: Dict[str, Any] = {}
            try:
                # P1-12: 删除误清缓存（aggregator 已预取，不应在循环中reset）
                exit_sigs = te.check_exit_signals(
                    stock_code=code,
                    stock_name=name,
                    market_mode=market_mode,
                    sector_status=sector_status,
                    sector_name=sector_name,
                )
                exit_signals = exit_sigs or []
                # 从 timing_engine 缓存中提取技术面数据
                tech_data = te._tech_data_full.get(code, {})
            except Exception as e:
                logger.warning("持仓 %s(%s) 出场检查失败: %s", name, code, e)

            # 健康度评级（基于出场信号紧急度）
            rating = self._rate_health(exit_signals)

            # 浮盈亏比例（若有 cost 字段则计算，否则 0）
            pnl_ratio = 0.0
            cost = h.get("cost", 0)
            current_price = tech_data.get("current_price", 0)
            if cost > 0 and current_price > 0:
                pnl_ratio = (current_price - cost) / cost

            health = HoldingHealth(
                stock_code=code,
                stock_name=name,
                rating=rating,
                pnl_ratio=pnl_ratio,
                mode_adjustment=mode_adjustment,
                sector_status=sector_status,
                sector_name=sector_name,
                exit_signals=exit_signals,
                details=tech_data,
                should_push=bool(exit_signals),
                push_reason="；".join(
                    f"[{s.exit_type}]{getattr(s, 'reason', '')}"
                    for s in exit_signals
                ) if exit_signals else "",
            )
            results.append(health)

        logger.info("持仓分析完成: %d 只，有出场信号 %d 只",
                    len(results), sum(1 for r in results if r.exit_signals))
        return results

    @staticmethod
    def _rate_health(exit_signals: List) -> str:
        """根据出场信号紧急度评级"""
        if not exit_signals:
            return "健康"
        # 紧急 > 重要 > 一般
        urgency_set = {getattr(s, "urgency", "") for s in exit_signals}
        exit_types = {getattr(s, "exit_type", "") for s in exit_signals}

        if "紧急" in urgency_set or "破位止损" in exit_types:
            return "危险"
        if "破位预警" in exit_types:
            return "警告"
        return "观察"


class _SkillCooldownStub:
    """
    问财技能冷却桩

    原工程 aggregator 通过 self._position_analyzer._skill._api_cooldown_until
    检查问财 API 配额。PositionAnalyzer 重构后不再依赖问财，但保留桩对象
    以兼容该检查（永远返回 0 = 配额可用）。
    """
    _api_cooldown_until: float = 0.0


# 单例
_instance: Optional[PositionAnalyzer] = None


def get_position_analyzer() -> PositionAnalyzer:
    global _instance
    if _instance is None:
        _instance = PositionAnalyzer()
    return _instance


__all__ = ["PositionAnalyzer", "get_position_analyzer"]
