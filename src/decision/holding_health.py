"""
持仓健康度与自选分析结果数据结构

【新增模块 — P0 修复】
aggregator.py 的 SignalSummary 引用 HoldingHealth 与 WatchlistAnalysisResult
但原工程从未定义，导致模块导入即报 NameError。本模块补全定义。

设计原则：
- 字段对齐 aggregator._generate_pre_market_summary 的访问习惯
- 字段对齐 position_builder 加仓信号检查的输入需求
- 兼容旧代码的属性访问模式（不强制 dataclass）
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class HoldingHealth:
    """持仓健康度评估结果

    由 PositionAnalyzer.analyze_all_holdings 产出，描述单只持仓股的健康状态。
    """
    stock_code: str
    stock_name: str
    rating: str = "观察"             # 健康 / 观察 / 警告 / 危险
    pnl_ratio: float = 0.0           # 浮盈亏比例（+0.05 表示 +5%）
    mode_adjustment: str = ""        # 模式驱动的建议动作（如 "defend 减半仓"）
    sector_status: str = ""          # 板块状态：main_trend/rotational/retreating/unknown
    sector_name: str = ""
    exit_signals: List[Any] = field(default_factory=list)  # ExitSignal 列表
    details: Dict[str, Any] = field(default_factory=dict)  # 技术面+机构资金原始数据
    should_push: bool = False        # 是否推送
    push_reason: str = ""            # 推送原因

    def __post_init__(self):
        # 兼容属性赋值（防止外部代码用 holding.rating = "危险" 修改）
        pass


@dataclass
class WatchlistAnalysisResult:
    """自选股分析结果

    由 PositionAnalyzer.analyze_all_watchlist 产出（如有），或由 aggregator
    从 v3 信号合并构建。原工程 watchlist_analyses 已标记为 deprecated，
    但保留 dataclass 以兼容字段访问。
    """
    stock_code: str
    stock_name: str
    filter_result: Optional[Any] = None   # FilterResult 或 None
    entry_signals: List[Any] = field(default_factory=list)
    exit_signals: List[Any] = field(default_factory=list)
    should_push: bool = False
    push_reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


__all__ = ["HoldingHealth", "WatchlistAnalysisResult"]
