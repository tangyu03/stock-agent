"""
A股回测引擎 v2 — 主流计算逻辑对齐版

【重写要点】（与 Zipline/Backtrader/VectorBT 主流逻辑对齐）

1. **次日开盘成交**（消除前视偏差）
   - T 日收盘生成信号 → T+1 日开盘价成交
   - 信号生成与执行分离，符合实盘交易节奏
   - 涨跌停判断也用 T+1 日数据（避免用 T 日数据假判可执行性）

2. **完整 A 股约束**
   - T+1 卖出限制：买入当日不可卖出
   - 涨停无法买入 / 跌停无法卖出（一字板判定）
   - 100 股最小交易单位（LOT_SIZE）
   - 滑点（5bps）+ 佣金（0.025%）+ 印花税（仅卖 0.05%）

3. **完整指标输出**（与 metrics.py 对齐）
   - 收益类: 总收益、年化收益
   - 风险调整: Sharpe、Sortino、Calmar
   - 风险: 最大回撤、年化波动率、下行波动率
   - 基准对比: Alpha、Beta、信息比率、跟踪误差
   - 交易: 胜率、盈亏比、平均盈亏

4. **基准对比支持**
   - 支持传入基准 K 线（如沪深300），生成买入持有净值曲线
   - 自动计算 Alpha/Beta/IR

使用示例见 scripts/run_grid_search.py
"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import defaultdict

from .metrics import (
    Metrics, calc_all_metrics, build_benchmark_curve,
    print_metrics,
)

logger = logging.getLogger(__name__)


# ============================================================
# 交易成本参数（来自执行模型 skill 的 A 股推荐值）
# ============================================================

SLIPPAGE_BPS = 5.0           # 固定滑点 5bps（0.05%），适合散户小资金
COMMISSION_RATE = 0.00025    # 手续费 0.025%（券商通常水平，含规费）
STAMP_DUTY_RATE = 0.0005     # 印花税 0.05%（仅卖出，2023年8月减半后）
LOT_SIZE = 100               # A 股最小交易单位
TRADING_DAYS_PER_YEAR = 252  # A 股年交易日数


# ============================================================
# 涨跌停限制
# ============================================================

def get_limit_ratio(stock_code: str) -> float:
    """
    根据股票代码返回涨跌停比例

    - 创业板 300/301: 20%
    - 科创板 688/689: 20%
    - ST/*ST: 5%
    - 主板其他: 10%
    """
    code = stock_code.upper().strip()
    if code.startswith("300") or code.startswith("301"):
        return 0.20
    if code.startswith("688") or code.startswith("689"):
        return 0.20
    if code.startswith("ST") or code.startswith("*ST"):
        return 0.05
    return 0.10


def apply_slippage(price: float, direction: int, bps: float = SLIPPAGE_BPS) -> float:
    """
    固定滑点模型

    Args:
        price: 原始价格
        direction: 1=买入, -1=卖出
        bps: 滑点基点数

    Returns:
        滑点后的实际成交价（买入价更高，卖出价更低）
    """
    slippage = price * bps / 10000
    return price + direction * slippage


def calc_buy_cost(amount: float) -> float:
    """买入交易成本（仅佣金，无印花税）"""
    return amount * COMMISSION_RATE


def calc_sell_cost(amount: float) -> float:
    """卖出交易成本（佣金 + 印花税）"""
    return amount * COMMISSION_RATE + amount * STAMP_DUTY_RATE


def is_limit_up(stock_code: str, prev_close: float, current_close: float) -> bool:
    """判断是否一字涨停（无法买入）"""
    if prev_close <= 0:
        return False
    limit_ratio = get_limit_ratio(stock_code)
    change_ratio = (current_close - prev_close) / prev_close
    return change_ratio >= limit_ratio * 0.995


def is_limit_down(stock_code: str, prev_close: float, current_close: float) -> bool:
    """判断是否一字跌停（无法卖出）"""
    if prev_close <= 0:
        return False
    limit_ratio = get_limit_ratio(stock_code)
    change_ratio = (current_close - prev_close) / prev_close
    return change_ratio <= -limit_ratio * 0.995


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Signal:
    """
    交易信号

    【执行时点】T 日收盘生成信号 → T+1 日开盘成交
    - date: 信号生成日期（T 日）
    - price: 信号触发价（T 日收盘价，仅作为参考）
    - 实际成交价: T+1 日开盘价（含滑点）
    """
    date: str           # 信号生成日期 YYYY-MM-DD（T 日）
    code: str           # 股票代码
    action: str         # "buy" 或 "sell"
    shares: int         # 股数（必须是 100 的倍数）
    price: float        # 信号触发价（T 日收盘价，参考用）
    reason: str = ""    # 信号原因
    kline_patterns: list = None


@dataclass
class Trade:
    """实际成交记录"""
    signal_date: str       # 信号生成日期（T 日）
    fill_date: str         # 实际成交日期（T+1 日）
    code: str
    action: str
    shares: int
    signal_price: float    # 信号触发价
    fill_price: float      # 实际成交价（T+1 开盘 + 滑点）
    cost: float            # 手续费+印花税
    amount: float          # 成交金额（不含费用）
    total: float           # 实际扣款/到账（含费用）
    pnl_pct: float = 0.0   # 卖出时的盈亏%（仅 sell 有效）
    reason: str = ""


@dataclass
class Position:
    """持仓"""
    code: str
    shares: int = 0
    cost_price: float = 0.0
    buy_date: str = ""       # 最近一次买入日期（用于 T+1 判断）


@dataclass
class BacktestResult:
    """回测结果（含完整指标）"""
    # 基础
    initial_cash: float
    final_value: float

    # 完整指标集
    metrics: Metrics = field(default_factory=Metrics)
    benchmark_metrics: Optional[Metrics] = None  # 基准指标（可选）

    # 明细
    trades: List[Trade] = field(default_factory=list)
    daily_values: List[Dict] = field(default_factory=list)
    benchmark_daily_values: List[Dict] = field(default_factory=list)
    skipped_signals: List[Dict] = field(default_factory=list)

    # 兼容字段（用于旧脚本读取）
    @property
    def total_return(self) -> float:
        return self.final_value - self.initial_cash

    @property
    def total_return_pct(self) -> float:
        return self.metrics.total_return_pct

    @property
    def annual_return_pct(self) -> float:
        return self.metrics.annual_return_pct

    @property
    def sharpe_ratio(self) -> float:
        return self.metrics.sharpe_ratio

    @property
    def max_drawdown_pct(self) -> float:
        return self.metrics.max_drawdown_pct

    @property
    def win_rate(self) -> float:
        return self.metrics.win_rate

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def buy_count(self) -> int:
        return sum(1 for t in self.trades if t.action == "buy")

    @property
    def sell_count(self) -> int:
        return sum(1 for t in self.trades if t.action == "sell")

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_signals)

    @property
    def winning_trades(self) -> int:
        return self.metrics.winning_trades

    @property
    def losing_trades(self) -> int:
        return self.metrics.losing_trades

    @property
    def avg_win_pct(self) -> float:
        return self.metrics.avg_win_pct

    @property
    def avg_loss_pct(self) -> float:
        return self.metrics.avg_loss_pct


# ============================================================
# 回测引擎 v2
# ============================================================

class BacktestEngine:
    """
    A 股回测引擎 v2

    核心改进（与主流回测框架对齐）：
    1. **T+1 次日开盘成交**（消除前视偏差）
       - T 日信号 → T+1 日开盘价成交
       - 信号生成与执行分离
    2. **涨跌停判定用 T+1 数据**（避免假判可执行性）
    3. **完整指标输出**（Sharpe/Sortino/Calmar/Alpha/Beta/IR）
    4. **基准对比支持**

    使用方法：
        engine = BacktestEngine(initial_cash=1_000_000)
        result = engine.run(
            signals=signals,
            kline_data=kline_data,
            benchmark_kline=csi300_kline,  # 可选
        )
    """

    def __init__(
        self,
        initial_cash: float = 1_000_000,
        slippage_bps: float = SLIPPAGE_BPS,
        commission_rate: float = COMMISSION_RATE,
        stamp_duty_rate: float = STAMP_DUTY_RATE,
        risk_free_rate: float = 0.0,
    ):
        """
        Args:
            initial_cash: 初始资金
            slippage_bps: 滑点（基点）
            commission_rate: 佣金费率
            stamp_duty_rate: 印花税费率（仅卖出）
            risk_free_rate: 年化无风险利率（默认 0）
        """
        self.initial_cash = initial_cash
        self.slippage_bps = slippage_bps
        self.commission_rate = commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.risk_free_rate = risk_free_rate

        self.cash = initial_cash
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.skipped_signals: List[Dict] = []
        self._sell_pnl_pcts: List[float] = []

    def reset(self):
        """重置引擎状态（复用同一引擎跑多次时调用）"""
        self.cash = self.initial_cash
        self.positions = {}
        self.trades = []
        self.skipped_signals = []
        self._sell_pnl_pcts = []

    def run(
        self,
        signals: List[Signal],
        kline_data: Dict[str, List[Dict]],
        benchmark_kline: Optional[List[Dict]] = None,
    ) -> BacktestResult:
        """
        执行回测

        Args:
            signals: 交易信号列表（按时间顺序）
            kline_data: 每只股票的 K 线数据
                {code: [{"date", "open", "close", "high", "low", "volume", "prev_close"}, ...]}
            benchmark_kline: 基准指数 K 线（可选，用于计算 Alpha/Beta/IR）

        Returns:
            BacktestResult
        """
        logger.info(
            "开始回测：初始资金 %s，信号数 %d，执行时点=T+1次日开盘",
            self.initial_cash, len(signals),
        )

        # 1. 构建 K 线索引
        # {code: {date: kline_row}}  — 用于按日期查 K 线
        # {code: [date_list]}        — 用于查找下一交易日
        kline_index: Dict[str, Dict[str, Dict]] = {}
        date_seq_by_code: Dict[str, List[str]] = {}
        for code, rows in kline_data.items():
            kline_index[code] = {row["date"]: row for row in rows}
            date_seq_by_code[code] = [row["date"] for row in rows]

        # 2. 按日期分组信号
        # 信号是 T 日生成，需要在 T+1 日执行
        signals_by_date: Dict[str, List[Signal]] = defaultdict(list)
        for sig in signals:
            signals_by_date[sig.date].append(sig)

        # 3. 获取所有涉及的交易日（按日期排序）
        all_dates_set = set()
        for rows in kline_data.values():
            for row in rows:
                all_dates_set.add(row["date"])
        all_dates = sorted(all_dates_set)

        # 4. 构建"下一交易日"映射
        # next_trading_day[T] = T+1（按 all_dates 全局排序）
        date_to_idx = {d: i for i, d in enumerate(all_dates)}
        next_trading_day_cache: Dict[str, Optional[str]] = {}

        def get_next_trading_day(d: str) -> Optional[str]:
            """获取 d 的下一个交易日"""
            if d in next_trading_day_cache:
                return next_trading_day_cache[d]
            idx = date_to_idx.get(d)
            if idx is None or idx + 1 >= len(all_dates):
                next_trading_day_cache[d] = None
                return None
            nxt = all_dates[idx + 1]
            next_trading_day_cache[d] = nxt
            return nxt

        # 5. 逐日处理
        # 对每个交易日 d：
        #   (a) 执行"信号生成日 = d - 1"的信号（即在 d 日开盘成交）
        #   (b) 收盘后 mark-to-market
        # 修复 BUG-B10: 停牌日用最近收盘价估值
        daily_values = []
        last_close_by_code: Dict[str, float] = {}  # 记录每只股票最近可用收盘价
        for d in all_dates:
            # (a) 执行 T-1 日生成的信号（T 日开盘成交）
            prev_day = all_dates[date_to_idx[d] - 1] if date_to_idx[d] > 0 else None
            if prev_day:
                for sig in signals_by_date.get(prev_day, []):
                    self._process_signal_t_plus_1(
                        sig, fill_date=d, kline_index=kline_index,
                        get_next_day=get_next_trading_day,
                    )

            # (b) 当日收盘 mark-to-market（修复 BUG-B10: 停牌日用最近收盘价）
            total_value = self.cash
            for code, pos in self.positions.items():
                if pos.shares > 0:
                    if code in kline_index and d in kline_index[code]:
                        close = float(kline_index[code][d].get("close", 0) or 0)
                        if close > 0:
                            last_close_by_code[code] = close
                            total_value += pos.shares * close
                        elif code in last_close_by_code:
                            total_value += pos.shares * last_close_by_code[code]
                    elif code in last_close_by_code:
                        # 停牌日：用最近可用收盘价估值
                        total_value += pos.shares * last_close_by_code[code]
            daily_values.append({"date": d, "value": round(total_value, 2)})

        # 修复 BUG-B9: 处理最后一个交易日生成的信号（尝试 T+1 执行，找不到则跳过）
        last_day = all_dates[-1] if all_dates else None
        if last_day:
            last_day_signals = signals_by_date.get(last_day, [])
            for sig in last_day_signals:
                next_day = get_next_trading_day(last_day)
                if next_day and next_day in kline_index.get(sig.code, {}):
                    self._process_signal_t_plus_1(
                        sig, fill_date=next_day, kline_index=kline_index,
                        get_next_day=get_next_trading_day,
                    )
                else:
                    self._skip(sig, last_day, "最后一个交易日的信号无 T+1 日可执行")

        # 6. 生成基准净值曲线
        benchmark_curve = []
        if benchmark_kline:
            start_d = all_dates[0] if all_dates else ""
            end_d = all_dates[-1] if all_dates else ""
            benchmark_curve = build_benchmark_curve(
                benchmark_kline, self.initial_cash, start_d, end_d,
            )

        # 7. 计算所有指标
        metrics = calc_all_metrics(
            daily_values=daily_values,
            initial_cash=self.initial_cash,
            sell_pnl_pcts=self._sell_pnl_pcts,
            benchmark_daily_values=benchmark_curve if benchmark_curve else None,
            risk_free_rate=self.risk_free_rate,
        )

        # 基准自身指标（用于对比）
        benchmark_metrics = None
        if benchmark_curve:
            benchmark_metrics = calc_all_metrics(
                daily_values=benchmark_curve,
                initial_cash=self.initial_cash,
                sell_pnl_pcts=None,
                benchmark_daily_values=None,
                risk_free_rate=self.risk_free_rate,
            )

        result = BacktestResult(
            initial_cash=self.initial_cash,
            final_value=round(daily_values[-1]["value"], 2) if daily_values else self.cash,
            metrics=metrics,
            benchmark_metrics=benchmark_metrics,
            trades=self.trades,
            daily_values=daily_values,
            benchmark_daily_values=benchmark_curve,
            skipped_signals=self.skipped_signals,
        )

        logger.info(
            "回测完成：最终市值 %.2f，夏普 %.4f，最大回撤 %.2f%%，Alpha %.2f%%，Beta %.4f",
            result.final_value, metrics.sharpe_ratio,
            metrics.max_drawdown_pct, metrics.alpha, metrics.beta,
        )
        return result

    # ============================================================
    # 信号处理（T+1 次日开盘成交）
    # ============================================================

    def _process_signal_t_plus_1(
        self,
        sig: Signal,
        fill_date: str,
        kline_index: Dict[str, Dict[str, Dict]],
        get_next_day,
    ):
        """
        在 fill_date 当日执行 sig（成交价为 fill_date 开盘价）

        Args:
            sig: T-1 日生成的信号
            fill_date: T 日（成交日）
            kline_index: K 线索引
            get_next_day: 函数，返回下一交易日
        """
        code = sig.code
        if code not in kline_index:
            self._skip(sig, fill_date, "无 K 线数据")
            return

        fill_kline = kline_index[code].get(fill_date)
        if not fill_kline:
            self._skip(sig, fill_date, "成交日无 K 线数据")
            return

        # 实际成交价 = fill_date 开盘价（前视偏差已消除）
        fill_open = float(fill_kline.get("open", 0) or 0)
        if fill_open <= 0:
            self._skip(sig, fill_date, "成交日开盘价无效")
            return

        prev_close = float(fill_kline.get("prev_close", 0) or fill_kline.get("open", 0))

        # T+1 检查：卖出时，买入必须是昨天或更早
        if sig.action == "sell":
            pos = self.positions.get(code)
            if not pos or pos.shares <= 0:
                self._skip(sig, fill_date, "无持仓")
                return
            if pos.buy_date == fill_date:
                self._skip(sig, fill_date, "T+1 约束：当日买入不可卖出")
                return

        # 修复 BUG-B11: 涨跌停检查用开盘价（实际成交价）而非收盘价
        # 一字板判定：开盘价就是涨跌停价（全天锁死）
        if sig.action == "buy" and is_limit_up(code, prev_close, fill_open):
            chg = (fill_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
            self._skip(sig, fill_date, f"一字涨停，无法买入（开盘涨幅 {chg:.2f}%）")
            return
        if sig.action == "sell" and is_limit_down(code, prev_close, fill_open):
            chg = (fill_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
            self._skip(sig, fill_date, f"一字跌停，无法卖出（开盘跌幅 {chg:.2f}%）")
            return

        # 执行
        if sig.action == "buy":
            self._execute_buy(sig, fill_date, fill_open)
        elif sig.action == "sell":
            self._execute_sell(sig, fill_date, fill_open)

    def _execute_buy(self, sig: Signal, fill_date: str, fill_open: float):
        """执行买入（fill_price = fill_open + slippage）"""
        fill_price = apply_slippage(fill_open, direction=1, bps=self.slippage_bps)
        shares = (sig.shares // LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            self._skip(sig, fill_date, "取整后股数为 0")
            return

        amount = fill_price * shares
        cost = amount * self.commission_rate
        total_deduction = amount + cost

        # 资金不足调整
        if total_deduction > self.cash:
            affordable_shares = int(
                (self.cash / (fill_price * (1 + self.commission_rate))) // LOT_SIZE
            ) * LOT_SIZE
            if affordable_shares <= 0:
                self._skip(sig, fill_date,
                           f"资金不足（需 {total_deduction:.2f}，仅有 {self.cash:.2f}）")
                return
            shares = affordable_shares
            amount = fill_price * shares
            cost = amount * self.commission_rate
            total_deduction = amount + cost

        # 更新持仓
        pos = self.positions.get(sig.code)
        if pos is None:
            pos = Position(code=sig.code)
            self.positions[sig.code] = pos

        old_value = pos.shares * pos.cost_price
        new_total_shares = pos.shares + shares
        pos.cost_price = (old_value + amount) / new_total_shares if new_total_shares > 0 else 0
        pos.shares = new_total_shares
        pos.buy_date = fill_date

        self.cash -= total_deduction

        trade = Trade(
            signal_date=sig.date, fill_date=fill_date,
            code=sig.code, action="buy",
            shares=shares, signal_price=sig.price, fill_price=fill_price,
            cost=cost, amount=amount, total=total_deduction,
            reason=sig.reason,
        )
        self.trades.append(trade)
        logger.debug(
            "买入 %s %d股 @ %.2f（信号日 %s，成交日 %s，开盘 %.2f + 滑点）",
            sig.code, shares, fill_price, sig.date, fill_date, fill_open,
        )

    def _execute_sell(self, sig: Signal, fill_date: str, fill_open: float):
        """执行卖出（fill_price = fill_open - slippage）"""
        pos = self.positions.get(sig.code)
        if not pos or pos.shares <= 0:
            self._skip(sig, fill_date, "无持仓")
            return

        # shares=0 表示卖出全部持仓（信号生成器不跟踪持仓，由引擎决定）
        if sig.shares <= 0:
            sell_shares = pos.shares
        else:
            sell_shares = min(sig.shares, pos.shares)

        fill_price = apply_slippage(fill_open, direction=-1, bps=self.slippage_bps)
        shares = (sell_shares // LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            self._skip(sig, fill_date, "取整后股数为 0")
            return

        amount = fill_price * shares
        cost = amount * self.commission_rate + amount * self.stamp_duty_rate
        net_proceeds = amount - cost

        # 盈亏%
        cost_price = pos.cost_price
        pnl_pct = ((fill_price - cost_price) / cost_price * 100) if cost_price > 0 else 0

        pos.shares -= shares
        if pos.shares == 0:
            pos.cost_price = 0

        self.cash += net_proceeds

        trade = Trade(
            signal_date=sig.date, fill_date=fill_date,
            code=sig.code, action="sell",
            shares=shares, signal_price=sig.price, fill_price=fill_price,
            cost=cost, amount=amount, total=net_proceeds,
            pnl_pct=round(pnl_pct, 2),
            reason=sig.reason,
        )
        self.trades.append(trade)
        self._sell_pnl_pcts.append(round(pnl_pct, 2))
        logger.debug(
            "卖出 %s %d股 @ %.2f（信号日 %s，成交日 %s），盈亏 %.2f%%",
            sig.code, shares, fill_price, sig.date, fill_date, pnl_pct,
        )

    def _skip(self, sig: Signal, fill_date: str, reason: str):
        """记录被跳过的信号"""
        self.skipped_signals.append({
            "signal_date": sig.date,
            "fill_date": fill_date,
            "code": sig.code,
            "action": sig.action,
            "shares": sig.shares,
            "price": sig.price,
            "reason": reason,
        })
        logger.debug("跳过信号 %s %s %s（成交日 %s）：%s",
                     sig.date, sig.action, sig.code, fill_date, reason)


# ============================================================
# 结果打印（兼容旧接口）
# ============================================================

def print_result(result: BacktestResult, title: str = "回测结果"):
    """打印回测结果（使用新指标模块）"""
    print_metrics(result.metrics, title=title)

    if result.benchmark_metrics:
        print()
        print("─" * 70)
        print("  📊 基准对比")
        print("─" * 70)
        print(f"  {'指标':<14} {'策略':>14} {'基准':>14} {'超额':>14}")
        print("  " + "-" * 64)
        for name, attr, fmt in [
            ("总收益率%", "total_return_pct", "{:+.2f}%"),
            ("年化收益%", "annual_return_pct", "{:+.2f}%"),
            ("夏普比率", "sharpe_ratio", "{:.4f}"),
            ("最大回撤%", "max_drawdown_pct", "{:.2f}%"),
            ("年化波动%", "annual_volatility_pct", "{:.2f}%"),
        ]:
            s = getattr(result.metrics, attr)
            b = getattr(result.benchmark_metrics, attr)
            s_str = fmt.format(s)
            b_str = fmt.format(b)
            diff = s - b
            diff_str = f"{diff:+.4f}"
            print(f"  {name:<14} {s_str:>14} {b_str:>14} {diff_str:>14}")
        print("─" * 70)
        print(f"  Alpha:     {result.metrics.alpha:>+10.2f}%")
        print(f"  Beta:      {result.metrics.beta:>10.4f}")
        print(f"  信息比率:  {result.metrics.information_ratio:>10.4f}")
        print(f"  跟踪误差:  {result.metrics.tracking_error_pct:>10.2f}%")
        print("─" * 70)
