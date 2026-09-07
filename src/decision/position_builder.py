"""
加仓信号构建器
对已持有或自选已关注的标的，在盘中跳水/缩量回踩时发出加仓信号。

核心原则：
- 加仓 ≠ 进场 — 前提是已有底仓或已有判断
- 递进仓位 — 每次加仓是对前次判断的确认
- 退潮板块不加仓

仓位递进（由 PositionBuilder.calculate_progressive_position 计算）：
  首次加仓 ≤ 单票上限×30%
  二次加仓 ≤ 单票上限×30%（前次未破止损）
  三次加仓 ≤ 单票上限×40%（前两次都确认）

【P0 修复】
1. 删除原 line 42-44 的空 @dataclass 装饰器（Python 语法错误）
2. 补全 create_add_plan() / append_add_plan() 方法（aggregator.py 调用但未定义）
3. 加仓计划持久化到 portfolio.yaml 的 add_plans 字段
4. add_level 由 portfolio.yaml 中已有 add_plans 的层级推导（替代原工程未实现的真实持仓读取）
"""
import logging
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import uuid

import yaml

logger = logging.getLogger(__name__)


@dataclass
class PositionBuildSignal:
    """加仓信号"""
    stock_code: str
    stock_name: str
    signal_type: str           # arbitrage_add（套利加仓）/ focus_add（聚焦加仓）
    trigger_price: float       # 触发价位
    stop_loss: float           # 加仓后止损价
    add_level: int             # 加仓层级：1=首次, 2=二次, 3=三次
    suggested_shares: int      # 建议加仓股数
    suggested_amount: float    # 建议加仓金额
    total_holding_after: float # 加仓后总持仓股数
    trigger_reason: str        # 触发原因
    confidence: str = "中"     # 高/中/低
    sector_status: str = ""    # 板块状态
    tech_data: Dict = field(default_factory=dict)  # 技术面数据，供推送模板使用


@dataclass
class AddPlan:
    """啄米加仓计划（持久化到 portfolio.yaml）"""
    plan_id: str
    stock_code: str
    stock_name: str
    created_at: str
    entry_price: float
    status: str = "active"     # active / completed / cancelled
    levels: List[Dict] = field(default_factory=list)  # [{level, target_price, source, verify_conditions, executed, verified}]


class PositionBuilder:
    """加仓信号构建器（P0 修复版）"""

    def __init__(self):
        from ..config_models import load_config
        self._position_config = load_config("position.yaml").get("position", {})
        self._single_stock_max = self._position_config.get("single_stock_max", 0.25)
        # 递进比例：首次/二次/三次 各占单票上限的比例
        self._add_ratios = [0.30, 0.30, 0.40]

        # 总资产（用于将比例换算为金额/股数）
        self._total_asset = self._position_config.get("total_asset", 1_000_000)

        # portfolio.yaml 路径（用于持久化 add_plans）
        from ..config_models import CONFIG_DIR
        self._portfolio_path = os.path.join(CONFIG_DIR, "portfolio.yaml")

        # 内存缓存：plan_id → AddPlan
        self._plans_cache: Dict[str, AddPlan] = {}
        self._load_plans()

    # ============================================================
    # 加仓计划管理（P0 新增）
    # ============================================================

    def create_add_plan(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        tech_data: Optional[Dict] = None,
    ) -> Optional[AddPlan]:
        """
        为新进场信号创建啄米加仓计划

        三级加仓目标：
        - Level 1: MA20 支撑
        - Level 2: MA60 支撑
        - Level 3: 前低支撑

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            entry_price: 进场价（触发价）
            tech_data: 技术面数据（含 MA20/MA60/recent_low）

        Returns:
            AddPlan 对象，或 None（已存在活跃计划时返回 None）
        """
        # 同股已有活跃计划 → 跳过
        for plan in self._plans_cache.values():
            if (plan.stock_code == stock_code
                    and plan.status == "active"):
                return None

        tech_data = tech_data or {}
        ma20 = tech_data.get("ma20") or entry_price
        ma60 = tech_data.get("ma60") or entry_price * 0.95
        recent_low = tech_data.get("recent_low") or entry_price * 0.90

        today = datetime.now().strftime("%Y-%m-%d")
        plan_id = f"ap_{stock_code}_{today.replace('-','')}_{uuid.uuid4().hex[:6]}"

        verify_conditions = [
            "板块未退潮（main_trend / rotational）",
            "无新增利空事件",
            "价格未破上一级止损",
            "量价未背离",
        ]

        levels = [
            {
                "level": 1,
                "target_price": round(float(ma20), 2),
                "source": "MA20",
                "verify_conditions": verify_conditions,
                "executed": False,
                "verified": False,
            },
            {
                "level": 2,
                "target_price": round(float(ma60), 2),
                "source": "MA60",
                "verify_conditions": verify_conditions,
                "executed": False,
                "verified": False,
            },
            {
                "level": 3,
                "target_price": round(float(recent_low), 2),
                "source": "前低",
                "verify_conditions": verify_conditions,
                "executed": False,
                "verified": False,
            },
        ]

        plan = AddPlan(
            plan_id=plan_id,
            stock_code=stock_code,
            stock_name=stock_name,
            created_at=today,
            entry_price=round(float(entry_price), 2),
            status="active",
            levels=levels,
        )

        logger.info("创建加仓计划 %s: %s(%s) entry=%.2f levels=%d",
                    plan_id, stock_name, stock_code, entry_price, len(levels))
        return plan

    def append_add_plan(self, plan: AddPlan) -> bool:
        """将加仓计划追加到 portfolio.yaml 持久化"""
        if not plan or not plan.plan_id:
            return False

        # 内存缓存
        self._plans_cache[plan.plan_id] = plan

        # 持久化到 YAML
        try:
            with open(self._portfolio_path, "r", encoding="utf-8") as f:
                portfolio = yaml.safe_load(f) or {}
            add_plans = portfolio.get("add_plans") or []

            # 序列化为 YAML 兼容字典
            plan_dict = {
                "plan_id": plan.plan_id,
                "stock_code": plan.stock_code,
                "stock_name": plan.stock_name,
                "created_at": plan.created_at,
                "entry_price": plan.entry_price,
                "status": plan.status,
                "levels": plan.levels,
            }
            add_plans.append(plan_dict)
            portfolio["add_plans"] = add_plans

            with open(self._portfolio_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(portfolio, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

            logger.info("加仓计划 %s 已持久化到 portfolio.yaml", plan.plan_id)
            return True
        except Exception as e:
            logger.error("持久化加仓计划失败: %s", e)
            return False

    def get_active_plans(self, stock_code: Optional[str] = None) -> List[AddPlan]:
        """获取活跃加仓计划"""
        plans = [p for p in self._plans_cache.values() if p.status == "active"]
        if stock_code:
            plans = [p for p in plans if p.stock_code == stock_code]
        return plans

    def get_next_add_level(self, stock_code: str) -> int:
        """
        获取指定股票的下一加仓层级（1/2/3）

        基于该股票活跃计划中已 executed 的 level 数量推导：
        - 0 个 executed → level 1
        - 1 个 executed → level 2
        - 2 个 executed → level 3
        - 3 个 executed → 0（不再加仓）
        """
        plans = self.get_active_plans(stock_code)
        if not plans:
            return 1  # 无计划时默认 level 1（首次加仓）

        plan = plans[0]
        executed_count = sum(1 for lv in plan.levels if lv.get("executed"))
        if executed_count >= len(plan.levels):
            return 0
        return executed_count + 1

    # ============================================================
    # 加仓信号生成
    # ============================================================

    def calculate_progressive_position(
        self,
        add_level: int,
        total_asset: Optional[float] = None,
    ) -> Tuple[float, int]:
        """
        计算递进仓位

        Args:
            add_level: 加仓层级 1/2/3
            total_asset: 总资产（None 用配置默认值）

        Returns:
            (suggested_amount, suggested_shares)
        """
        if add_level < 1 or add_level > 3:
            return 0.0, 0

        total = total_asset or self._total_asset
        ratio = self._add_ratios[add_level - 1]
        single_stock_amount = total * self._single_stock_max
        suggested_amount = single_stock_amount * ratio

        # 100 股最小单位（A 股规则）
        # 这里无法知道股价，返回金额，股数由调用方计算
        return round(suggested_amount, 2), 0

    def _load_plans(self):
        """从 portfolio.yaml 加载已有 add_plans 到内存缓存"""
        try:
            with open(self._portfolio_path, "r", encoding="utf-8") as f:
                portfolio = yaml.safe_load(f) or {}
            for plan_dict in (portfolio.get("add_plans") or []):
                if not isinstance(plan_dict, dict):
                    continue
                plan = AddPlan(
                    plan_id=plan_dict.get("plan_id", ""),
                    stock_code=plan_dict.get("stock_code", ""),
                    stock_name=plan_dict.get("stock_name", ""),
                    created_at=plan_dict.get("created_at", ""),
                    entry_price=plan_dict.get("entry_price", 0.0),
                    status=plan_dict.get("status", "active"),
                    levels=plan_dict.get("levels", []),
                )
                if plan.plan_id:
                    self._plans_cache[plan.plan_id] = plan
            if self._plans_cache:
                logger.info("加载 %d 个加仓计划", len(self._plans_cache))
        except FileNotFoundError:
            logger.debug("portfolio.yaml 不存在，跳过加仓计划加载")
        except Exception as e:
            logger.warning("加载加仓计划失败: %s", e)


# 单例
_instance: Optional[PositionBuilder] = None


def get_position_builder() -> PositionBuilder:
    global _instance
    if _instance is None:
        _instance = PositionBuilder()
    return _instance


__all__ = ["PositionBuildSignal", "AddPlan", "PositionBuilder", "get_position_builder"]
