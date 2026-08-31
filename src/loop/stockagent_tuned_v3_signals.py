"""
stock-agent 策略信号生成 v4 — 回测逻辑与实盘逻辑一致

【核心设计原则】

1. 信号生成器不跟踪持仓状态
   - 每天对所有股票生成入场和出场信号
   - 买入/卖出只是信号，与持仓无关
   - 回测引擎根据信号模拟交易，跟踪持仓和资金

2. 出场逻辑完全由 TimingEngine 负责
   - 破位止损 / 破位预警 / 冲高止盈（衰竭信号）/ 板块退潮
   - MA5 压制止盈是纯技术面判断（跌破 MA5 且 MA5 上升），不需要持仓成本

3. 回测只负责测参数，不额外加出场判断
   - 所有阈值从 config/timing.yaml 读取
   - 网格搜索的最优参数可直接迁移到实盘

【信号执行时点】
T 日收盘生成信号 → T+1 日开盘成交（由 BacktestEngine 处理）
"""

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# 默认回测参数
# ============================================================
# 只有仓位管理参数，没有出场判断参数
# 出场逻辑全部由 TimingEngine.check_exit_signals 负责

DEFAULT_BACKTEST_PARAMS = {
    "budget_per_stock": 250_000,
}


# 股票代码 → 所属板块映射（回测简化版）
STOCK_SECTOR_MAP = {
    "688256": "半导体", "688041": "半导体",
    "301005": "航天军工", "301232": "航天军工",
    "300308": "光模块", "300502": "光模块",
    "688820": "半导体", "688321": "生物医药",
    "688037": "半导体", "688110": "存储芯片",
    "688521": "半导体", "600367": "化学原料",
}


class StockAgentTunedV3Signals:
    """
    策略信号生成 v4（回测驱动器 — 委托给 TimingEngine）

    信号生成时机：T 日收盘
    信号执行时机：T+1 日开盘（由 BacktestEngine 处理）

    不跟踪持仓状态，每天对所有股票生成入场和出场信号。
    回测引擎收到全部信号后自行决定执行哪些。
    """

    def __init__(
        self,
        market_mode: str = "defend",
        params: dict = None,
        adaptive_mode: bool = False,
        index_kline: List[Dict] = None,
        sector_state_map: Dict[str, str] = None,
    ):
        self.market_mode = market_mode
        self.params = {**DEFAULT_BACKTEST_PARAMS, **(params or {})}
        self.adaptive_mode = adaptive_mode
        self.index_kline = index_kline or []
        self._sector_state_map = sector_state_map or {}

        # 创建回测专用 TimingEngine 实例
        timing_override = self._extract_timing_override(self.params)
        from ..analyzers.timing_engine import get_backtest_timing_engine
        self._timing = get_backtest_timing_engine(params_override=timing_override)

        # 自适应模式预计算
        self._mode_series: Dict[str, str] = {}
        if adaptive_mode and index_kline:
            from .market_mode_adaptive import get_market_mode_adaptive
            self._mode_adaptive = get_market_mode_adaptive()
            self._mode_series = self._mode_adaptive.get_mode_series(index_kline)
            logger.info("自适应模式已启用，预计算 %d 天的 mode", len(self._mode_series))

    @staticmethod
    def _extract_timing_override(params: dict) -> dict:
        """从 params 中提取属于 timing_engine 的覆盖参数"""
        backtest_only_keys = set(DEFAULT_BACKTEST_PARAMS.keys())
        timing_override = {}
        for k, v in params.items():
            if k not in backtest_only_keys and k != "backtest_mode":
                timing_override[k] = v
        return timing_override

    def generate_signals(self, kline_data: Dict[str, List[Dict]]) -> List:
        """
        生成交易信号（委托给 TimingEngine）

        不跟踪持仓状态，每天对所有股票生成入场和出场信号。
        回测引擎收到全部信号后自行决定执行哪些。
        """
        try:
            from src.loop.backtest_engine import Signal
        except ImportError:
            from .backtest_engine import Signal

        # 提取所有交易日
        all_dates_set = set()
        for rows in kline_data.values():
            for row in rows:
                all_dates_set.add(row["date"])
        all_dates = sorted(all_dates_set)

        if not all_dates:
            return []

        all_signals = []
        budget = self.params["budget_per_stock"]

        # 遍历每个交易日
        for d in all_dates:
            # 切片当日 K 线（只包含当日有数据的股票）
            kline_sliced: Dict[str, List[Dict]] = {}
            for code, rows in kline_data.items():
                sliced = [r for r in rows if r["date"] <= d]
                # 当日必须有 K 线（停牌日不生成信号）
                if sliced and sliced[-1]["date"] == d and len(sliced) >= 20:
                    kline_sliced[code] = sliced

            if not kline_sliced:
                continue

            # 指数 K 线切片
            index_sliced = [r for r in self.index_kline if r["date"] <= d] if self.index_kline else []

            # 设置回测上下文
            self._timing.set_backtest_context(d, kline_sliced, index_sliced)

            # 自适应模式
            if self.adaptive_mode and self._mode_series:
                current_mode = self._mode_series.get(d, self.market_mode)
            else:
                current_mode = self.market_mode

            # ────────── 对每只股票生成入场和出场信号 ──────────
            for code, kline in kline_sliced.items():
                sector = STOCK_SECTOR_MAP.get(code, "")
                sector_status = self._get_sector_state(sector, d)

                # 入场信号（对所有股票，不检查是否持仓）
                entry_signals = self._timing.check_entry_signals(
                    stock_code=code,
                    stock_name="",
                    market_mode=current_mode,
                    sector_status=sector_status,
                )
                if entry_signals:
                    esig = entry_signals[0]
                    price = float(kline[-1].get("收盘", kline[-1].get("close", 0)) or 0)
                    if price > 0:
                        shares = int((budget / price) // 100) * 100
                        if shares > 0:
                            all_signals.append(Signal(
                                date=d, code=code, action="buy",
                                shares=shares, price=price,
                                reason=f"[买入]{esig.entry_type}\n  {esig.trigger_reason}",
                                kline_patterns=esig.tech_data.get("kline_pattern", []) if esig.tech_data else [],
                            ))

                # 出场信号（对所有股票，不检查是否持仓）
                exit_signals = self._timing.check_exit_signals(
                    stock_code=code,
                    stock_name="",
                    market_mode=current_mode,
                    sector_status=sector_status,
                    sector_name=sector,
                )
                if exit_signals:
                    # 取最紧急的信号
                    urgency_rank = {"紧急": 0, "重要": 1, "常规": 2, "观察": 3}
                    exit_signals.sort(key=lambda s: urgency_rank.get(s.urgency, 99))
                    esig = exit_signals[0]
                    price = float(kline[-1].get("收盘", kline[-1].get("close", 0)) or 0)
                    if price > 0:
                        all_signals.append(Signal(
                            date=d, code=code, action="sell",
                            shares=0,  # 不指定股数，引擎根据持仓决定
                            price=price,
                            reason=f"[卖出]{esig.exit_type}\n  {esig.reason}",
                            kline_patterns=esig.tech_data.get("kline_pattern", []) if esig.tech_data else [],
                        ))

        return all_signals

    # ============================================================
    # 板块状态
    # ============================================================

    def _get_sector_state(self, sector: str, date: str = "") -> str:
        """获取板块状态"""
        if not sector:
            return "unknown"
        if self._sector_state_map:
            return self._sector_state_map.get(sector, "unknown")
        return "rotational"
