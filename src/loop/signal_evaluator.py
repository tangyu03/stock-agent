"""
信号事件研究法评估器

与系统设计完全一致：信号就是信号，不依赖持仓/成本/股数。
只评估信号本身的预测能力。

【买入信号质量】
  对每个买入信号，计算未来 N 天收益率
  - 胜率 = 收益率 > 0 的比例
  - 平均收益 = 所有信号收益率的均值
  - 期望值 = 胜率 × 平均盈利 - 败率 × 平均亏损

【卖出信号质量】
  对每个卖出信号，计算"如果卖出"vs"如果不卖"的未来 N 天收益差
  - 卖出价值 = 不卖的收益 - 卖出的收益（正值说明卖出是对的）
  - 避免亏损率 = 卖出后下跌的比例

【综合评分】
  buy_score = 买入期望值（越高说明买入信号越准）
  sell_score = 卖出避免亏损率（越高说明卖出信号越准）
  total = buy_score × 0.6 + sell_score × 0.4
"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SignalQualityMetrics:
    """信号质量指标"""
    # 买入信号质量
    buy_signal_count: int = 0
    buy_win_rate: float = 0.0           # 买入后 N 天上涨的比例
    buy_avg_return: float = 0.0          # 买入后 N 天平均收益%
    buy_avg_win: float = 0.0             # 平均盈利%
    buy_avg_loss: float = 0.0            # 平均亏损%
    buy_expectancy: float = 0.0          # 期望值 = 胜率×平均盈利 - 败率×平均亏损

    # 卖出信号质量
    sell_signal_count: int = 0
    sell_avoid_loss_rate: float = 0.0    # 卖出后下跌的比例（卖出对了）
    sell_avg_avoided: float = 0.0        # 平均避免的亏损%（不卖-卖的收益差）
    sell_expectancy: float = 0.0         # 卖出期望值

    # 综合评分
    total_score: float = 0.0             # buy_expectancy*0.6 + sell_expectancy*0.4

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


class SignalEvaluator:
    """
    信号事件研究法评估器

    不模拟交易，只统计信号本身的预测能力。
    与持仓/成本/股数完全无关。
    """

    def __init__(self, hold_days: int = 5):
        """
        Args:
            hold_days: 评估窗口（买入后 N 天的收益）
        """
        self.hold_days = hold_days

    def evaluate(
        self,
        signals: List,
        kline_data: Dict[str, List[Dict]],
    ) -> SignalQualityMetrics:
        """
        评估信号质量

        Args:
            signals: Signal 列表（date/code/action/price）
            kline_data: {code: [{"date", "close", ...}, ...]}

        Returns:
            SignalQualityMetrics
        """
        # 构建 K 线索引：{code: {date: close}}
        close_index: Dict[str, Dict[str, float]] = {}
        date_seq: Dict[str, List[str]] = {}
        for code, rows in kline_data.items():
            close_index[code] = {r["date"]: float(r.get("close", 0) or 0) for r in rows}
            date_seq[code] = [r["date"] for r in rows]

        # 分离买入/卖出信号
        buy_signals = [s for s in signals if s.action == "buy"]
        sell_signals = [s for s in signals if s.action == "sell"]

        # 评估买入信号质量
        buy_returns = []
        for sig in buy_signals:
            ret = self._calc_future_return(sig, close_index, date_seq)
            if ret is not None:
                buy_returns.append(ret)

        # 评估卖出信号质量
        sell_avoids = []  # 卖出避免的亏损%
        sell_correct = 0  # 卖出后下跌的次数
        for sig in sell_signals:
            avoided = self._calc_sell_avoidance(sig, close_index, date_seq)
            if avoided is not None:
                sell_avoids.append(avoided)
                if avoided > 0:
                    sell_correct += 1

        # 计算指标
        m = SignalQualityMetrics()
        m.buy_signal_count = len(buy_returns)
        m.sell_signal_count = len(sell_avoids)

        # 买入指标
        if buy_returns:
            wins = [r for r in buy_returns if r > 0]
            losses = [r for r in buy_returns if r <= 0]
            m.buy_win_rate = len(wins) / len(buy_returns) * 100
            m.buy_avg_return = sum(buy_returns) / len(buy_returns)
            m.buy_avg_win = sum(wins) / len(wins) if wins else 0
            m.buy_avg_loss = sum(losses) / len(losses) if losses else 0
            m.buy_expectancy = (
                m.buy_win_rate / 100 * m.buy_avg_win
                + (1 - m.buy_win_rate / 100) * m.buy_avg_loss
            )

        # 卖出指标
        if sell_avoids:
            m.sell_avoid_loss_rate = sell_correct / len(sell_avoids) * 100
            m.sell_avg_avoided = sum(sell_avoids) / len(sell_avoids)
            # 卖出期望值 = 平均避免的亏损（正值=卖出有价值）
            m.sell_expectancy = m.sell_avg_avoided

        # 综合评分
        m.total_score = m.buy_expectancy * 0.6 + m.sell_expectancy * 0.4

        return m

    def _calc_future_return(
        self,
        sig,
        close_index: Dict[str, Dict[str, float]],
        date_seq: Dict[str, List[str]],
    ) -> Optional[float]:
        """
        计算买入信号后 N 天的收益率

        Returns:
            收益率%（正=赚钱，负=亏钱），None=无法计算
        """
        code = sig.code
        if code not in close_index or code not in date_seq:
            return None

        dates = date_seq[code]
        closes = close_index[code]

        # 找信号日索引
        try:
            sig_idx = dates.index(sig.date)
        except ValueError:
            return None

        # 找 N 天后的索引
        future_idx = sig_idx + self.hold_days
        if future_idx >= len(dates):
            return None  # 数据不足

        entry_price = sig.price  # 信号日收盘价
        future_price = closes.get(dates[future_idx], 0)
        if entry_price <= 0 or future_price <= 0:
            return None

        return (future_price - entry_price) / entry_price * 100

    def _calc_sell_avoidance(
        self,
        sig,
        close_index: Dict[str, Dict[str, float]],
        date_seq: Dict[str, List[str]],
    ) -> Optional[float]:
        """
        计算卖出信号避免的亏损

        卖出价值 = 不卖的收益 - 卖出的收益
        - 正值：卖出对了（避免了亏损或锁定了利润）
        - 负值：卖出错了（卖了之后又涨了）

        简化评估：
        - 不卖的收益 = 信号日到 N 天后的收益
        - 卖出的收益 = 0（卖出后不持有）
        - 避免的亏损 = 不卖的收益（正值=不卖赚钱了→卖错了；负值=不卖亏了→卖对了）

        但这样"避免的亏损"语义是"不卖会亏多少"，正值才说明卖出对了。
        所以：avoided = -未来收益（未来跌了→avoided 为正→卖出对了）

        Returns:
            避免的亏损%（正值=卖出对了，负值=卖出错了），None=无法计算
        """
        code = sig.code
        if code not in close_index or code not in date_seq:
            return None

        dates = date_seq[code]
        closes = close_index[code]

        try:
            sig_idx = dates.index(sig.date)
        except ValueError:
            return None

        future_idx = sig_idx + self.hold_days
        if future_idx >= len(dates):
            return None

        entry_price = sig.price  # 信号日收盘价
        future_price = closes.get(dates[future_idx], 0)
        if entry_price <= 0 or future_price <= 0:
            return None

        future_return = (future_price - entry_price) / entry_price * 100
        # 卖出避免的亏损 = -未来收益
        # 未来跌了（future_return < 0）→ avoided > 0 → 卖出对了
        # 未来涨了（future_return > 0）→ avoided < 0 → 卖出错了
        return -future_return


def print_signal_metrics(m: SignalQualityMetrics, title: str = "信号质量评估"):
    """打印信号质量指标"""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print("  📈 买入信号质量:")
    print(f"     信号数:       {m.buy_signal_count}")
    print(f"     胜率:         {m.buy_win_rate:.2f}%")
    print(f"     平均收益:     {m.buy_avg_return:+.2f}%")
    print(f"     平均盈利:     {m.buy_avg_win:+.2f}%")
    print(f"     平均亏损:     {m.buy_avg_loss:+.2f}%")
    print(f"     期望值:       {m.buy_expectancy:+.4f}")
    print()
    print("  📉 卖出信号质量:")
    print(f"     信号数:       {m.sell_signal_count}")
    print(f"     避免亏损率:   {m.sell_avoid_loss_rate:.2f}%")
    print(f"     平均避免亏损: {m.sell_avg_avoided:+.2f}%")
    print(f"     期望值:       {m.sell_expectancy:+.4f}")
    print()
    print(f"  🏆 综合评分:     {m.total_score:+.4f}")
    print("     (买入期望×0.6 + 卖出期望×0.4)")
    print("=" * 60)
    print()
