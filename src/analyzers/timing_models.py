"""Data models for timing engine signals."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class EntrySignal:
    """入场信号"""
    stock_code: str
    stock_name: str
    entry_type: str               # 恐慌抄底 / 套利低吸 / 确认追强 / 价量突破
    entry_trigger_price: float
    stop_loss: float               # 系统自动计算
    target_type: str               # 主升持有 / 冲高止盈 / 持有观察
    target_range: List[float] = field(default_factory=list)  # 止盈区间
    position_level: str = "normal"  # normal / heavy
    applicable_modes: List[str] = field(default_factory=list)  # 适用操作模式
    sector_status: str = ""        # 板块状态
    sector_name: str = ""         # 板块名称
    sw_level2: str = ""           # 同花顺行业（原名申万二级，字段名保留兼容）
    sw_level3: str = ""           # 行业三级（字段名保留兼容）
    trigger_reason: str = ""       # 触发原因描述
    strategy_summary: str = ""     # 策略逻辑透传
    confidence: str = "中"         # 信号置信度
    benchmark_price: float = 0.0   # RRR/仓位唯一基准，禁止与现价混用
    rrr_low: Optional[float] = None
    rrr_high: Optional[float] = None
    kline_patterns: List[Dict] = field(default_factory=list)
    tech_data: Dict = field(default_factory=dict)
    execution_plan: Dict = field(default_factory=dict)
    # 【一】可证伪假说（X/Y/Z/W 四要素，出厂检查已通过才到达这里）
    hypothesis: Dict = field(default_factory=dict)
    # 【三】信号事件 ID（生命周期链接，回执后转 triggered）
    event_id: str = ""
    # 【三】受众：empty=仅空仓者成立 / holding=持仓者加仓参考
    audience: str = "empty"
    # 【二】基本面摘要（业绩雷/盈利质量/财报窗口，来自 fundamental_gate）
    fundamental_note: str = ""
    # 【Phase3】估值透镜摘要（估值泡沫/低基数反转/亏损分型/资金-分析师冲突）
    valuation_note: str = ""
    # 【Phase4 P1-1】防守模式降仓放行标记（三重门证据 + 外推量能）
    defensive_chase: Optional[Dict] = None
    # 事件出生时落审计：Y 不再只给结果，也给公式和输入。
    entry_y_formula: str = ""
    entry_y_inputs: Dict = field(default_factory=dict)


@dataclass
class ExitSignal:
    """出场信号"""
    stock_code: str
    stock_name: str
    exit_type: str                 # 破位止损 / 破位预警 / 冲高止盈 / MA5压制 / 技术走弱 / 板块退潮 / 策略兑现 / 信号作废 / 信号过期
    trigger_price: float
    stop_loss_price: float
    reason: str
    urgency: str = "重要"
    mode_constrained: bool = False
    sector_status: str = ""
    sector_name: str = ""
    sw_level3: str = ""
    tech_data: Dict = field(default_factory=dict)
    # 【四】配对出场标记：paired=策略原生 Z/W 硬触发 / system=旧系统兜底（降观察）
    source: str = "system"
    paired_strategy: str = ""
    # 只读引用事件库；卖出信号不得自行描述买入事件的阶段。
    event_id: str = ""

@dataclass
class StopLossCalc:
    """止损价计算结果"""
    stock_code: str
    current_price: float
    support_candidates: List[Dict[str, float]]
    chosen_support: float
    stop_loss_price: float
    resistance: float
    ladder: list = None  # C4: 阶梯止损 [{support, price, reduce_ratio}, ...]
