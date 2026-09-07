"""
Walk-Forward 优化框架

【主流对齐逻辑】

Walk-Forward 是主流量化策略参数优化方法，核心思想：
1. 用过去 N 天（训练窗口）数据搜索最优参数
2. 用接下来 M 天（测试窗口）数据验证参数表现（样本外）
3. 滚动推进：训练窗口向后移 step 天，再算一次
4. 最终聚合所有测试窗口结果，得到样本外真实表现

这种方法可有效防止过拟合，比在全样本上找最优更可靠。

【示例】

假设总数据 120 个交易日，train=60, test=20, step=20：

  Fold 1:  train[ 0:60]  →  test[60:80]
  Fold 2:  train[20:80]  →  test[80:100]
  Fold 3:  train[40:100] →  test[100:120]

  共 3 个 fold，输出每个 fold 的 IS/OOS Sharpe，并聚合 OOS 表现。

【使用方法】

    wf = WalkForwardOptimizer(
        kline_data=kline_data,
        benchmark_kline=csi300_kline,
        train_window=60,
        test_window=20,
        step=20,
        engine_factory=lambda: BacktestEngine(initial_cash=1_000_000),
        signal_factory=lambda params: StockAgentTunedV3Signals(market_mode="defend", params=params),
        objective="sharpe",  # sharpe | calmar | sortino | total_return
    )

    result = wf.run_grid_search(grid={
        "panic_min_conditions": [2, 3],
        "take_profit_threshold": [0.05, 0.08],
        ...
    })

    # result.is_metrics: 训练窗口最优指标
    # result.oos_metrics: 测试窗口聚合指标（真实样本外表现）
    # result.best_params: 训练窗口综合最优参数
    # result.folds: 每个 fold 的明细
"""
import logging
import itertools
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

from .metrics import Metrics

logger = logging.getLogger(__name__)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class FoldResult:
    """单个 fold 的结果"""
    fold_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_days: int
    test_days: int

    # 训练窗口（IS）的最优参数及其指标
    best_params: Dict[str, Any] = field(default_factory=dict)
    is_metrics: Optional[Metrics] = None  # In-Sample
    is_total_combos: int = 0

    # 测试窗口（OOS）的指标（用 best_params 跑）
    oos_metrics: Optional[Metrics] = None  # Out-of-Sample
    oos_signal_count: int = 0
    oos_trade_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "fold_idx": self.fold_idx,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "train_days": self.train_days,
            "test_days": self.test_days,
            "best_params": self.best_params,
            "is_metrics": self.is_metrics.to_dict() if self.is_metrics else None,
            "is_total_combos": self.is_total_combos,
            "oos_metrics": self.oos_metrics.to_dict() if self.oos_metrics else None,
            "oos_signal_count": self.oos_signal_count,
            "oos_trade_count": self.oos_trade_count,
        }


@dataclass
class WalkForwardResult:
    """Walk-Forward 优化结果"""
    # 配置
    train_window: int
    test_window: int
    step: int
    n_folds: int
    objective: str
    total_combinations: int  # 每个 fold 搜索的参数组合数

    # 所有 fold 明细
    folds: List[FoldResult] = field(default_factory=list)

    # 聚合 OOS 表现
    oos_aggregated: Optional[Metrics] = None  # 跨 fold 的聚合指标
    oos_sharpe_mean: float = 0.0
    oos_sharpe_std: float = 0.0
    oos_return_mean_pct: float = 0.0
    oos_return_std_pct: float = 0.0

    # IS 表现（参考，必然偏乐观）
    is_sharpe_mean: float = 0.0
    is_sharpe_std: float = 0.0

    # 综合最优参数（按 OOS 表现投票/聚合）
    best_params_overall: Dict[str, Any] = field(default_factory=dict)

    # 搜索时间
    search_seconds: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "config": {
                "train_window": self.train_window,
                "test_window": self.test_window,
                "step": self.step,
                "n_folds": self.n_folds,
                "objective": self.objective,
                "total_combinations": self.total_combinations,
                "search_seconds": round(self.search_seconds, 2),
            },
            "oos_aggregated": self.oos_aggregated.to_dict() if self.oos_aggregated else None,
            "oos_sharpe_mean": round(self.oos_sharpe_mean, 4),
            "oos_sharpe_std": round(self.oos_sharpe_std, 4),
            "oos_return_mean_pct": round(self.oos_return_mean_pct, 4),
            "oos_return_std_pct": round(self.oos_return_std_pct, 4),
            "is_sharpe_mean": round(self.is_sharpe_mean, 4),
            "is_sharpe_std": round(self.is_sharpe_std, 4),
            "best_params_overall": self.best_params_overall,
            "folds": [f.to_dict() for f in self.folds],
        }


# ============================================================
# Walk-Forward 优化器
# ============================================================

class WalkForwardOptimizer:
    """
    Walk-Forward 优化器

    Args:
        kline_data: {code: [{"date", "open", "close", ...}, ...]}
        benchmark_kline: 基准 K 线（可选）
        train_window: 训练窗口大小（交易日数）
        test_window: 测试窗口大小（交易日数）
        step: 滚动步长（交易日数）
        initial_cash: 初始资金
        engine_factory: 引擎工厂函数（每次调用返回新引擎）
        signal_factory: 信号生成器工厂函数，接受 params 返回 signal generator
        objective: 优化目标，"sharpe" | "calmar" | "sortino" | "total_return"
        risk_free_rate: 年化无风险利率
    """

    def __init__(
        self,
        kline_data: Dict[str, List[Dict]],
        benchmark_kline: Optional[List[Dict]] = None,
        train_window: int = 60,
        test_window: int = 20,
        step: int = 20,
        initial_cash: float = 1_000_000,
        engine_factory: Callable = None,
        signal_factory: Callable = None,
        objective: str = "sharpe",
        risk_free_rate: float = 0.0,
    ):
        self.kline_data = kline_data
        self.benchmark_kline = benchmark_kline or []
        self.train_window = train_window
        self.test_window = test_window
        self.step = step
        self.initial_cash = initial_cash
        self.engine_factory = engine_factory
        self.signal_factory = signal_factory
        self.objective = objective
        self.risk_free_rate = risk_free_rate

        # 提取所有交易日（按日期排序）
        all_dates_set = set()
        for rows in kline_data.values():
            for row in rows:
                all_dates_set.add(row["date"])
        self.all_dates = sorted(all_dates_set)

    # ============================================================
    # 主入口：网格搜索 + Walk-Forward
    # ============================================================

    def run_grid_search(self, grid: Dict[str, List]) -> WalkForwardResult:
        """
        在 Walk-Forward 框架下做网格搜索

        Args:
            grid: 参数网格，如 {"panic_min_conditions": [2, 3], ...}

        Returns:
            WalkForwardResult
        """
        start_time = datetime.now()
        n_folds = self._calc_n_folds()
        total_combos = 1
        for v in grid.values():
            total_combos *= len(v)

        logger.info(
            "Walk-Forward 网格搜索: %d folds, %d 组合/fold, 共 %d 次回测",
            n_folds, total_combos, n_folds * total_combos,
        )

        result = WalkForwardResult(
            train_window=self.train_window,
            test_window=self.test_window,
            step=self.step,
            n_folds=n_folds,
            objective=self.objective,
            total_combinations=total_combos,
        )

        # 逐 fold 处理
        for fold_idx in range(n_folds):
            fold = self._run_single_fold(fold_idx, grid)
            result.folds.append(fold)
            is_sharpe = fold.is_metrics.sharpe_ratio if fold.is_metrics else 0
            oos_sharpe = fold.oos_metrics.sharpe_ratio if fold.oos_metrics else 0
            logger.info(
                "Fold %d/%d: IS Sharpe=%.4f, OOS Sharpe=%.4f, params=%s",
                fold_idx + 1, n_folds, is_sharpe, oos_sharpe, fold.best_params,
            )

        # 聚合 OOS 表现
        self._aggregate_oos(result)

        # 选综合最优参数（每个 fold 投票）
        result.best_params_overall = self._vote_best_params(result.folds)

        result.search_seconds = (datetime.now() - start_time).total_seconds()
        return result

    # ============================================================
    # 单个 fold 处理
    # ============================================================

    def _run_single_fold(self, fold_idx: int, grid: Dict[str, List]) -> FoldResult:
        """跑单个 fold：训练窗口找最优 → 测试窗口验证"""
        train_start_idx = fold_idx * self.step
        train_end_idx = train_start_idx + self.train_window - 1
        test_start_idx = train_end_idx + 1
        test_end_idx = test_start_idx + self.test_window - 1

        if test_end_idx >= len(self.all_dates):
            test_end_idx = len(self.all_dates) - 1

        train_start_date = self.all_dates[train_start_idx]
        train_end_date = self.all_dates[train_end_idx]
        test_start_date = self.all_dates[test_start_idx]
        test_end_date = self.all_dates[test_end_idx]

        # 切分数据
        train_kline = self._slice_kline(self.kline_data, train_start_date, train_end_date)
        test_kline = self._slice_kline(self.kline_data, test_start_date, test_end_date)
        train_bench = self._slice_kline_single(self.benchmark_kline, train_start_date, train_end_date)
        test_bench = self._slice_kline_single(self.benchmark_kline, test_start_date, test_end_date)

        # ───── 步骤 1：在训练窗口上网格搜索 ─────
        best_score = -float("inf")
        best_params = {}
        best_metrics = None

        keys = list(grid.keys())
        value_lists = [grid[k] for k in keys]
        all_combos = list(itertools.product(*value_lists))

        for combo in all_combos:
            params = dict(combo._asdict()) if hasattr(combo, "_asdict") else dict(zip(keys, combo, strict=False))  # 保持原宽松行为

            try:
                metrics = self._evaluate(
                    params=params,
                    kline_data=train_kline,
                    benchmark_kline=train_bench,
                )
            except Exception as e:
                logger.debug("训练评估失败: %s, params=%s", e, params)
                continue

            score = self._get_objective_score(metrics)
            if score > best_score:
                best_score = score
                best_params = params
                best_metrics = metrics

        # ───── 步骤 2：在测试窗口上用 best_params 验证 ─────
        oos_metrics = None
        oos_signal_count = 0
        oos_trade_count = 0
        if best_params and test_kline:
            try:
                oos_metrics = self._evaluate(
                    params=best_params,
                    kline_data=test_kline,
                    benchmark_kline=test_bench,
                    return_signal_count=True,
                )
                if isinstance(oos_metrics, tuple):
                    oos_metrics, oos_signal_count = oos_metrics
                # 取交易次数
                engine = self.engine_factory()
                gen = self.signal_factory(best_params)
                signals = gen.generate_signals(test_kline)
                oos_signal_count = len(signals)
                result_obj = engine.run(signals, test_kline, test_bench)
                oos_trade_count = len(result_obj.trades)
            except Exception as e:
                logger.warning("测试评估失败 fold %d: %s", fold_idx, e)

        return FoldResult(
            fold_idx=fold_idx,
            train_start=train_start_date,
            train_end=train_end_date,
            test_start=test_start_date,
            test_end=test_end_date,
            train_days=self.train_window,
            test_days=self.test_window,
            best_params=best_params,
            is_metrics=best_metrics,
            is_total_combos=len(all_combos),
            oos_metrics=oos_metrics,
            oos_signal_count=oos_signal_count,
            oos_trade_count=oos_trade_count,
        )

    # ============================================================
    # 工具函数
    # ============================================================

    def _calc_n_folds(self) -> int:
        """计算可生成的 fold 数"""
        total_days = len(self.all_dates)
        # train + test <= total_days，每 fold 推进 step
        if total_days < self.train_window + self.test_window:
            return 0
        remaining = total_days - self.train_window - self.test_window
        return remaining // self.step + 1

    def _slice_kline(
        self,
        kline_data: Dict[str, List[Dict]],
        start_date: str,
        end_date: str,
    ) -> Dict[str, List[Dict]]:
        """切分多只股票的 K 线数据到日期范围"""
        result = {}
        for code, rows in kline_data.items():
            sliced = [r for r in rows if start_date <= r["date"] <= end_date]
            if sliced:
                result[code] = sliced
        return result

    def _slice_kline_single(
        self,
        kline: List[Dict],
        start_date: str,
        end_date: str,
    ) -> List[Dict]:
        """切分单只股票/指数的 K 线数据"""
        return [r for r in kline if start_date <= r.get("date", "") <= end_date]

    def _evaluate(
        self,
        params: Dict,
        kline_data: Dict[str, List[Dict]],
        benchmark_kline: Optional[List[Dict]] = None,
        return_signal_count: bool = False,
    ) -> Metrics:
        """跑单次回测，返回 Metrics"""
        engine = self.engine_factory()
        gen = self.signal_factory(params)
        signals = gen.generate_signals(kline_data)

        if not signals:
            # 无信号时返回空 Metrics
            return Metrics()

        result = engine.run(signals, kline_data, benchmark_kline)
        return result.metrics

    def _get_objective_score(self, m: Metrics) -> float:
        """根据优化目标提取分数"""
        if self.objective == "sharpe":
            return m.sharpe_ratio
        elif self.objective == "calmar":
            return m.calmar_ratio
        elif self.objective == "sortino":
            return m.sortino_ratio
        elif self.objective == "total_return":
            return m.total_return_pct
        else:
            return m.sharpe_ratio

    def _aggregate_oos(self, result: WalkForwardResult):
        """聚合所有 fold 的 OOS 表现"""
        oos_metrics_list = [f.oos_metrics for f in result.folds if f.oos_metrics]
        if not oos_metrics_list:
            return

        # 简单平均（每个 fold 等权）
        n = len(oos_metrics_list)
        result.oos_sharpe_mean = sum(m.sharpe_ratio for m in oos_metrics_list) / n
        result.oos_sharpe_std = (
            sum((m.sharpe_ratio - result.oos_sharpe_mean) ** 2 for m in oos_metrics_list) / n
        ) ** 0.5
        result.oos_return_mean_pct = sum(m.total_return_pct for m in oos_metrics_list) / n
        result.oos_return_std_pct = (
            sum((m.total_return_pct - result.oos_return_mean_pct) ** 2 for m in oos_metrics_list) / n
        ) ** 0.5

        # IS 平均
        is_metrics_list = [f.is_metrics for f in result.folds if f.is_metrics]
        if is_metrics_list:
            n_is = len(is_metrics_list)
            result.is_sharpe_mean = sum(m.sharpe_ratio for m in is_metrics_list) / n_is
            result.is_sharpe_std = (
                sum((m.sharpe_ratio - result.is_sharpe_mean) ** 2 for m in is_metrics_list) / n_is
            ) ** 0.5

        # 聚合 Metrics（平均值）
        agg = Metrics()
        agg.total_return_pct = result.oos_return_mean_pct
        agg.sharpe_ratio = result.oos_sharpe_mean
        agg.max_drawdown_pct = sum(m.max_drawdown_pct for m in oos_metrics_list) / n
        agg.annual_return_pct = sum(m.annual_return_pct for m in oos_metrics_list) / n
        agg.calmar_ratio = sum(m.calmar_ratio for m in oos_metrics_list) / n
        agg.sortino_ratio = sum(m.sortino_ratio for m in oos_metrics_list) / n
        agg.win_rate = sum(m.win_rate for m in oos_metrics_list) / n
        agg.profit_factor = sum(m.profit_factor for m in oos_metrics_list) / n
        agg.alpha = sum(m.alpha for m in oos_metrics_list) / n
        agg.beta = sum(m.beta for m in oos_metrics_list) / n
        agg.information_ratio = sum(m.information_ratio for m in oos_metrics_list) / n
        agg.trade_count = sum(m.trade_count for m in oos_metrics_list)
        agg.winning_trades = sum(m.winning_trades for m in oos_metrics_list)
        agg.losing_trades = sum(m.losing_trades for m in oos_metrics_list)
        result.oos_aggregated = agg

    def _vote_best_params(self, folds: List[FoldResult]) -> Dict[str, Any]:
        """
        投票选综合最优参数

        规则：按 OOS Sharpe 加权，每个 fold 的 best_params 投一票，
        票数最多的参数值胜出（每个参数维度独立投票）
        """
        if not folds:
            return {}

        # 收集所有 fold 的 best_params
        param_keys = set()
        weighted_votes = {}  # {param_key: {value: weighted_score}}
        for f in folds:
            if not f.best_params or not f.oos_metrics:
                continue
            weight = max(0.01, f.oos_metrics.sharpe_ratio)  # 用 Sharpe 作权重，最小 0.01
            for k, v in f.best_params.items():
                param_keys.add(k)
                if k not in weighted_votes:
                    weighted_votes[k] = {}
                # 不可哈希的值（如 list）转字符串
                v_key = str(v) if not isinstance(v, (int, float, str, bool)) else v
                weighted_votes[k][v_key] = weighted_votes[k].get(v_key, 0) + weight

        best_overall = {}
        for k in param_keys:
            if k in weighted_votes and weighted_votes[k]:
                best_value = max(weighted_votes[k].items(), key=lambda x: x[1])[0]
                best_overall[k] = best_value
        return best_overall


# ============================================================
# 工具：构建参数网格（支持随机搜索子采样）
# ============================================================

def build_grid_combinations(grid: Dict[str, List]) -> List[Dict]:
    """
    构建网格参数组合列表

    Args:
        grid: {"param1": [v1, v2], "param2": [v1, v2, v3]}

    Returns:
        [{"param1": v1, "param2": v1}, {"param1": v1, "param2": v2}, ...]
    """
    keys = list(grid.keys())
    value_lists = [grid[k] for k in keys]
    combos = []
    for combo in itertools.product(*value_lists):
        combos.append(dict(zip(keys, combo, strict=False)))
    return combos
