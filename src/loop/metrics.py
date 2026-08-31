"""
标准化回测指标计算模块

主流量化回测指标（与 Zipline / Backtrader / Pyfolio / VectorBT 对齐）：

1. 收益类
   - total_return_pct:        总收益率%
   - annual_return_pct:       年化收益率%（252 交易日复利）

2. 风险调整收益类
   - sharpe_ratio:            夏普比率 = (年化收益 - 无风险利率) / 年化波动
   - sortino_ratio:           Sortino = (年化收益 - 无风险利率) / 下行波动
   - calmar_ratio:            Calmar = 年化收益 / 最大回撤

3. 风险类
   - max_drawdown_pct:        最大回撤%
   - annual_volatility_pct:   年化波动率%
   - downside_volatility_pct: 下行波动率%

4. 基准对比类
   - alpha:                   CAPM Alpha（年化）
   - beta:                    CAPM Beta
   - information_ratio:       IR = Alpha / 跟踪误差
   - tracking_error_pct:      跟踪误差%

5. 交易类
   - win_rate:                胜率%
   - profit_factor:           盈亏比 = 总盈利 / 总亏损
   - avg_win_pct / avg_loss_pct

无风险利率默认 0（A 股短期国债利率可后续接入），可通过参数覆盖。
"""
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import math


# ============================================================
# 常量
# ============================================================

TRADING_DAYS_PER_YEAR = 252  # A 股年交易日数（与 Zipline/Backtrader 一致）


# ============================================================
# 指标结果数据结构
# ============================================================

@dataclass
class Metrics:
    """标准化回测指标集"""
    # 收益类
    total_return_pct: float = 0.0
    annual_return_pct: float = 0.0

    # 风险调整收益类
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    # 风险类
    max_drawdown_pct: float = 0.0
    annual_volatility_pct: float = 0.0
    downside_volatility_pct: float = 0.0

    # 基准对比类（基准为 None 时全 0）
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error_pct: float = 0.0

    # 交易类
    win_rate: float = 0.0                # 胜率% = winning / (winning+losing+flat) * 100
    profit_factor: float = 0.0           # = total_wins / total_losses（总盈利/总亏损）
    avg_win_pct: float = 0.0             # 平均盈利幅度（%，正数）
    avg_loss_pct: float = 0.0            # 平均亏损幅度（%，负数）
    trade_count: int = 0                 # 总交易笔数 = winning + losing + flat
    winning_trades: int = 0              # 盈利笔数（pnl > 0）
    losing_trades: int = 0               # 亏损笔数（pnl < 0）
    flat_trades: int = 0                 # 持平笔数（pnl == 0，含进分母但不计胜负）
    win_loss_ratio: float = 0.0          # = avg_win / |avg_loss|（均盈/均亏比，与 profit_factor 区分）
    expectancy_per_trade: float = 0.0    # 期望收益%/笔 = win_rate/100 * avg_win + (1-win_rate/100) * avg_loss

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


# ============================================================
# 核心计算函数
# ============================================================

def daily_returns_from_values(daily_values: List[Dict]) -> List[float]:
    """
    从每日净值序列计算日收益率序列

    Args:
        daily_values: [{"date": "YYYY-MM-DD", "value": float}, ...]

    Returns:
        [r1, r2, ...] 日收益率列表（长度 = len(values) - 1）
    """
    if len(daily_values) < 2:
        return []
    returns = []
    for i in range(1, len(daily_values)):
        prev = daily_values[i - 1]["value"]
        curr = daily_values[i]["value"]
        if prev > 0:
            returns.append((curr - prev) / prev)
        else:
            returns.append(0.0)
    return returns


def calc_total_return_pct(initial_cash: float, final_value: float) -> float:
    """总收益率%"""
    if initial_cash <= 0:
        return 0.0
    return (final_value - initial_cash) / initial_cash * 100


def calc_annual_return_pct(initial_cash: float, final_value: float, n_days: int) -> float:
    """
    年化收益率%（复利，按 252 交易日）

    公式: (final/initial)^(252/n_days) - 1
    """
    if initial_cash <= 0 or n_days <= 1:
        return 0.0
    ratio = final_value / initial_cash
    if ratio <= 0:
        return -100.0
    return (ratio ** (TRADING_DAYS_PER_YEAR / n_days) - 1) * 100


def calc_sharpe(daily_returns: List[float], risk_free_rate: float = 0.0) -> float:
    """
    夏普比率（年化）

    公式: (mean_daily - rf_daily) / std_daily * sqrt(252)
         rf_daily = (1 + risk_free_rate)^(1/252) - 1 ≈ risk_free_rate / 252

    Args:
        daily_returns: 日收益率序列
        risk_free_rate: 年化无风险利率（如 0.02 = 2%），默认 0
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0
    rf_daily = (1 + risk_free_rate) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    excess = [r - rf_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    var = sum((e - mean_excess) ** 2 for e in excess) / len(excess)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean_excess / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def calc_sortino(daily_returns: List[float], risk_free_rate: float = 0.0) -> float:
    """
    Sortino 比率（年化，仅惩罚下行波动）

    公式: (mean_daily - rf_daily) / downside_std * sqrt(252)
         downside_std = sqrt(mean(min(0, r - target)^2))
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0
    rf_daily = (1 + risk_free_rate) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    excess = [r - rf_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    downside = [min(0, e) ** 2 for e in excess]
    downside_var = sum(downside) / len(downside)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0:
        return 0.0
    return mean_excess / downside_std * math.sqrt(TRADING_DAYS_PER_YEAR)


def calc_max_drawdown(daily_values: List[Dict]) -> Tuple[float, str, str]:
    """
    最大回撤%

    Returns:
        (max_drawdown_pct, peak_date, trough_date)
    """
    if not daily_values:
        return 0.0, "", ""
    peak = daily_values[0]["value"]
    peak_date = daily_values[0]["date"]
    max_dd = 0.0
    max_dd_peak_date = peak_date
    max_dd_trough_date = peak_date

    for dv in daily_values:
        v = dv["value"]
        if v > peak:
            peak = v
            peak_date = dv["date"]
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_peak_date = peak_date
                max_dd_trough_date = dv["date"]
    return max_dd * 100, max_dd_peak_date, max_dd_trough_date


def calc_volatility(daily_returns: List[float]) -> float:
    """年化波动率%"""
    if not daily_returns or len(daily_returns) < 2:
        return 0.0
    mean_r = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100


def calc_downside_volatility(daily_returns: List[float]) -> float:
    """下行波动率%（仅计算负收益）"""
    if not daily_returns:
        return 0.0
    neg_returns = [r for r in daily_returns if r < 0]
    if not neg_returns:
        return 0.0
    var = sum(r ** 2 for r in neg_returns) / len(neg_returns)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100


def calc_alpha_beta(
    strategy_returns: List[float],
    benchmark_returns: List[float],
    risk_free_rate: float = 0.0,
) -> Tuple[float, float, float, float]:
    """
    CAPM Alpha/Beta 计算

    Args:
        strategy_returns: 策略日收益率
        benchmark_returns: 基准日收益率（同长度）
        risk_free_rate: 年化无风险利率

    Returns:
        (alpha_annual, beta, tracking_error_pct, information_ratio)
        alpha_annual: 年化 Alpha（%）
        beta: Beta 系数
        tracking_error_pct: 年化跟踪误差（%）
        information_ratio: 信息比率 = alpha_daily / tracking_error_daily * sqrt(252)
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0

    rf_daily = (1 + risk_free_rate) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    s = [r - rf_daily for r in strategy_returns[:n]]
    b = [r - rf_daily for r in benchmark_returns[:n]]

    mean_s = sum(s) / n
    mean_b = sum(b) / n

    # Beta = Cov(s, b) / Var(b)
    cov = sum((s[i] - mean_s) * (b[i] - mean_b) for i in range(n)) / n
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n)) / n
    beta = cov / var_b if var_b > 0 else 0.0

    # Alpha（日度）= mean_s - beta * mean_b
    alpha_daily = mean_s - beta * mean_b
    alpha_annual = alpha_daily * TRADING_DAYS_PER_YEAR * 100  # 转年化%

    # 跟踪误差 = std(s - b) * sqrt(252)
    diff = [s[i] - b[i] for i in range(n)]
    mean_diff = sum(diff) / n
    te_var = sum((d - mean_diff) ** 2 for d in diff) / n
    te_daily = math.sqrt(te_var)
    tracking_error_pct = te_daily * math.sqrt(TRADING_DAYS_PER_YEAR) * 100

    # 信息比率 = mean(diff) / std(diff) * sqrt(252)
    information_ratio = (mean_diff / te_daily * math.sqrt(TRADING_DAYS_PER_YEAR)) if te_daily > 0 else 0.0

    return alpha_annual, beta, tracking_error_pct, information_ratio


def calc_trade_stats(sell_pnl_pcts: List[float]) -> Dict:
    """
    交易胜率/盈亏比统计

    口径说明（A1 修正后统一）：
      - win  = pnl > 0  （正盈利算胜）
      - loss = pnl < 0  （负亏损算负）
      - flat = pnl == 0 （恰好为 0，含进胜率分母但不计胜负）
      - 胜率 = winning / (winning + losing + flat) * 100
      - 期望 = win_rate/100 * avg_win + (1 - win_rate/100) * avg_loss
      - profit_factor = total_wins / total_losses（总盈利额/总亏损额，与 win_loss_ratio 不同）
      - win_loss_ratio = avg_win / |avg_loss|（均盈/均亏比）

    Args:
        sell_pnl_pcts: 每笔卖出的盈亏百分比列表

    Returns:
        {
            "win_rate": float,
            "profit_factor": float,
            "win_loss_ratio": float,
            "avg_win_pct": float,
            "avg_loss_pct": float,
            "winning_trades": int,
            "losing_trades": int,
            "flat_trades": int,
            "trade_count": int,
            "expectancy_per_trade": float,
        }
    """
    if not sell_pnl_pcts:
        return {
            "win_rate": 0.0, "profit_factor": 0.0, "win_loss_ratio": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "winning_trades": 0, "losing_trades": 0, "flat_trades": 0,
            "trade_count": 0, "expectancy_per_trade": 0.0,
        }

    # 三分类：胜 / 负 / 平
    wins = [p for p in sell_pnl_pcts if p > 0]
    losses = [p for p in sell_pnl_pcts if p < 0]
    flats = [p for p in sell_pnl_pcts if p == 0]

    total_wins = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.0

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0  # 负数
    avg_flat = (sum(flats) / len(flats)) if flats else 0.0     # 接近 0

    n_total = len(sell_pnl_pcts)
    win_rate = (len(wins) / n_total * 100) if n_total else 0.0
    profit_factor = (total_wins / total_losses) if total_losses > 0 else (float("inf") if total_wins > 0 else 0.0)
    win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)
    # 期望必须三分类：胜+负+平，等价于全样本均值 sum(pnl)/n
    # 错误做法：(win_rate/100)*avg_win + (1-win_rate/100)*avg_loss 会把 flat 当作 loss 处理
    if n_total > 0:
        expectancy = (len(wins) / n_total) * avg_win + (len(losses) / n_total) * avg_loss + (len(flats) / n_total) * avg_flat
    else:
        expectancy = 0.0

    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "win_loss_ratio": win_loss_ratio,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "flat_trades": len(flats),
        "trade_count": n_total,
        "expectancy_per_trade": expectancy,
    }


# ============================================================
# 综合指标计算（一次性输出所有指标）
# ============================================================

def calc_all_metrics(
    daily_values: List[Dict],
    initial_cash: float,
    sell_pnl_pcts: Optional[List[float]] = None,
    benchmark_daily_values: Optional[List[Dict]] = None,
    risk_free_rate: float = 0.0,
) -> Metrics:
    """
    一次性计算所有指标

    Args:
        daily_values: 策略每日净值 [{"date": ..., "value": ...}, ...]
        initial_cash: 初始资金
        sell_pnl_pcts: 每笔卖出的盈亏百分比
        benchmark_daily_values: 基准每日净值（同长度，对齐日期）
        risk_free_rate: 年化无风险利率

    Returns:
        Metrics 对象
    """
    m = Metrics()

    if not daily_values:
        return m

    final_value = daily_values[-1]["value"]
    n_days = len(daily_values)

    # 收益类
    m.total_return_pct = round(calc_total_return_pct(initial_cash, final_value), 4)
    m.annual_return_pct = round(calc_annual_return_pct(initial_cash, final_value, n_days), 4)

    # 日收益率
    dr = daily_returns_from_values(daily_values)

    # 风险调整类
    m.sharpe_ratio = round(calc_sharpe(dr, risk_free_rate), 4)
    m.sortino_ratio = round(calc_sortino(dr, risk_free_rate), 4)

    # 风险类
    mdd, _, _ = calc_max_drawdown(daily_values)
    m.max_drawdown_pct = round(mdd, 4)
    m.annual_volatility_pct = round(calc_volatility(dr), 4)
    m.downside_volatility_pct = round(calc_downside_volatility(dr), 4)
    m.calmar_ratio = round(
        m.annual_return_pct / m.max_drawdown_pct if m.max_drawdown_pct > 0 else 0.0, 4
    )

    # 基准对比类（修复 BUG-B12: 按日期对齐策略与基准的日收益率）
    if benchmark_daily_values and len(benchmark_daily_values) >= 2:
        # 按日期内连接对齐，避免日期错位导致 Alpha/Beta 无意义
        strat_by_date = {dv["date"]: dv["value"] for dv in daily_values}
        bench_by_date = {dv["date"]: dv["value"] for dv in benchmark_daily_values}
        common_dates = sorted(set(strat_by_date.keys()) & set(bench_by_date.keys()))
        if len(common_dates) >= 2:
            aligned_strat = [{"date": d, "value": strat_by_date[d]} for d in common_dates]
            aligned_bench = [{"date": d, "value": bench_by_date[d]} for d in common_dates]
            bench_dr = daily_returns_from_values(aligned_bench)
            strat_dr = daily_returns_from_values(aligned_strat)
            alpha, beta, te, ir = calc_alpha_beta(strat_dr, bench_dr, risk_free_rate)
            m.alpha = round(alpha, 4)
            m.beta = round(beta, 4)
            m.tracking_error_pct = round(te, 4)
            m.information_ratio = round(ir, 4)

    # 交易类
    if sell_pnl_pcts:
        ts = calc_trade_stats(sell_pnl_pcts)
        m.win_rate = round(ts["win_rate"], 4)
        m.profit_factor = round(ts["profit_factor"], 4) if ts["profit_factor"] != float("inf") else 999.99
        m.win_loss_ratio = round(ts["win_loss_ratio"], 4) if ts["win_loss_ratio"] != float("inf") else 999.99
        m.avg_win_pct = round(ts["avg_win_pct"], 4)
        m.avg_loss_pct = round(ts["avg_loss_pct"], 4)
        m.winning_trades = ts["winning_trades"]
        m.losing_trades = ts["losing_trades"]
        m.flat_trades = ts["flat_trades"]
        m.trade_count = ts["trade_count"]
        m.expectancy_per_trade = round(ts["expectancy_per_trade"], 4)

    return m


# ============================================================
# 基准净值序列生成
# ============================================================

def build_benchmark_curve(
    benchmark_kline: List[Dict],
    initial_cash: float,
    start_date: str = "",
    end_date: str = "",
) -> List[Dict]:
    """
    从基准指数 K 线生成"买入持有"净值曲线

    Args:
        benchmark_kline: 基准指数 K 线 [{"date": ..., "close": ...}, ...]
        initial_cash: 初始资金
        start_date: 起始日期（可选过滤）
        end_date: 结束日期（可选过滤）

    Returns:
        [{"date": ..., "value": ...}, ...] 基准净值曲线
    """
    if not benchmark_kline:
        return []

    # 过滤日期范围
    rows = benchmark_kline
    if start_date:
        rows = [r for r in rows if r.get("date", "") >= start_date]
    if end_date:
        rows = [r for r in rows if r.get("date", "") <= end_date]

    if not rows:
        return []

    base_close = float(rows[0].get("close", 0))
    if base_close <= 0:
        return []

    curve = []
    for r in rows:
        close = float(r.get("close", 0))
        if close <= 0:
            continue
        # 买入持有：净值 = initial_cash * (close / base_close)
        value = initial_cash * (close / base_close)
        curve.append({
            "date": r.get("date", ""),
            "value": round(value, 2),
        })
    return curve


def print_metrics(m: Metrics, title: str = "回测指标"):
    """打印指标（控制台友好版）"""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print(f"  📈 总收益率:     {m.total_return_pct:>+10.2f}%")
    print(f"  📅 年化收益率:   {m.annual_return_pct:>+10.2f}%")
    print()
    print(f"  📊 夏普比率:     {m.sharpe_ratio:>10.4f}  (>1 不错, >2 优秀)")
    print(f"  📊 Sortino:      {m.sortino_ratio:>10.4f}")
    print(f"  📊 Calmar:       {m.calmar_ratio:>10.4f}")
    print()
    print(f"  📉 最大回撤:     {m.max_drawdown_pct:>10.2f}%")
    print(f"  📉 年化波动率:   {m.annual_volatility_pct:>10.2f}%")
    print(f"  📉 下行波动率:   {m.downside_volatility_pct:>10.2f}%")
    print()
    if m.alpha != 0 or m.beta != 0:
        print(f"  🎯 Alpha:        {m.alpha:>+10.2f}%  (超额收益)")
        print(f"  🎯 Beta:         {m.beta:>10.4f}  (相对基准波动)")
        print(f"  🎯 信息比率:     {m.information_ratio:>10.4f}")
        print(f"  🎯 跟踪误差:     {m.tracking_error_pct:>10.2f}%")
        print()
    print(f"  🔄 交易次数:     {m.trade_count:>10d}  (胜{m.winning_trades}/负{m.losing_trades}/平{m.flat_trades})")
    print(f"  ✅ 盈利次数:     {m.winning_trades:>10d}")
    print(f"  ❌ 亏损次数:     {m.losing_trades:>10d}")
    print(f"  ⚪ 持平次数:     {m.flat_trades:>10d}")
    print(f"  🎯 胜率:         {m.win_rate:>10.2f}%")
    print(f"  📈 平均盈利:     {m.avg_win_pct:>+10.2f}%")
    print(f"  📉 平均亏损:     {m.avg_loss_pct:>+10.2f}%")
    print(f"  💰 盈亏比(PF):   {m.profit_factor:>10.4f}  (总盈利/总亏损)")
    print(f"  💎 均盈均亏比:   {m.win_loss_ratio:>10.4f}  (avg_win/|avg_loss|)")
    print(f"  🎯 期望收益/笔: {m.expectancy_per_trade:>+10.4f}%")
    print("=" * 70)
    print()
