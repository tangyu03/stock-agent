# -*- coding: utf-8 -*-
"""
用实际持仓跑网格搜索（快速版）

使用用户的 14 只实际持仓股票，2025-06-01 ~ 2026-07-10
Walk-Forward: train=80d / test=30d / step=30d
"""
import os
import sys
import logging
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

from src.loop.data_loader import DataLoader
from src.loop.backtest_engine import BacktestEngine
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS
from src.loop.walk_forward import WalkForwardOptimizer
from src.loop.metrics import print_metrics

# 实际持仓
STOCKS = [
    "688409", "688652", "300843", "688531", "688008", "688041",
    "688027", "301666", "002594", "920045", "688820", "688028",
    "300757", "688521",
]
START = "2025-06-01"
END = "2026-07-10"
CASH = 1_000_000

# 快速搜索空间（8 组合，先验证流程）
GRID = {
    "exit.exhaustion.rsi_overbought": [65, 70],
    "exit.exhaustion.strong_signal_min_count": [1, 2],
    "stop_loss.multiplier": [0.95, 0.97],
}


def _flatten_to_nested(flat):
    """扁平参数转嵌套 dict"""
    nested = {}
    for k, v in flat.items():
        parts = k.split(".")
        node = nested
        for p in parts[:-1]:
            if p not in node:
                node[p] = {}
            node = node[p]
        node[parts[-1]] = v
    return nested


def main():
    print("=" * 70)
    print("  实际持仓网格搜索（快速版）")
    print(f"  股票: {len(STOCKS)} 只")
    print(f"  时间: {START} ~ {END}")
    print("  Walk-Forward: train=80d / test=30d / step=30d")
    print(f"  搜索空间: {len(GRID)} 维, {eval('*'.join(str(len(v)) for v in GRID.values()))} 组合")
    print("=" * 70)

    # 加载数据
    print("\n📥 加载数据...")
    loader = DataLoader()
    kline_data = loader.load_kline(STOCKS, START, END)
    if not kline_data:
        print("❌ 数据加载失败")
        return
    total = sum(len(v) for v in kline_data.values())
    print(f"   ✅ {len(kline_data)} 只, {total} 根 K 线")

    # 加载沪深300基准
    bench_result = loader._akshare.get_index_data("000300")
    benchmark_kline = []
    if bench_result.success and bench_result.data:
        for r in bench_result.data:
            d = loader._parse_date(r.get("日期") or r.get("date") or "")
            if d and START <= d <= END:
                benchmark_kline.append({
                    "date": d,
                    "open": float(r.get("开盘", 0) or 0),
                    "close": float(r.get("收盘", 0) or 0),
                    "high": float(r.get("最高", 0) or 0),
                    "low": float(r.get("最低", 0) or 0),
                    "volume": float(r.get("成交量", 0) or 0),
                })
        print(f"   ✅ 沪深300: {len(benchmark_kline)} 根")
    else:
        print("   ⚠️ 沪深300加载失败")

    # Walk-Forward 搜索
    print("\n🔍 开始 Walk-Forward 网格搜索...")

    def engine_factory():
        return BacktestEngine(initial_cash=CASH)

    def signal_factory(flat_params):
        nested = _flatten_to_nested(flat_params)
        full_params = {**DEFAULT_BACKTEST_PARAMS, **nested}
        return StockAgentTunedV3Signals(market_mode="defend", params=full_params)

    wf = WalkForwardOptimizer(
        kline_data=kline_data,
        benchmark_kline=benchmark_kline,
        train_window=80,
        test_window=30,
        step=30,
        initial_cash=CASH,
        engine_factory=engine_factory,
        signal_factory=signal_factory,
        objective="sharpe",
    )

    result = wf.run_grid_search(grid=GRID)

    # 打印结果
    print(f"\n{'='*70}")
    print("  📊 Walk-Forward 搜索结果")
    print(f"{'='*70}")
    print(f"  总 fold 数: {result.n_folds}")
    print(f"  每折组合数: {result.total_combinations}")
    print(f"  总搜索耗时: {result.search_seconds:.1f}s")

    print("\n  📋 各 fold 明细:")
    print(f"  {'Fold':<6} {'训练区间':<26} {'测试区间':<26} {'IS Sharpe':>10} {'OOS Sharpe':>11} {'OOS 收益%':>11} {'OOS 交易':>8}")
    print("  " + "-" * 100)
    for f in result.folds:
        is_s = f.is_metrics.sharpe_ratio if f.is_metrics else 0
        oos_s = f.oos_metrics.sharpe_ratio if f.oos_metrics else 0
        oos_r = f.oos_metrics.total_return_pct if f.oos_metrics else 0
        oos_t = f.oos_trade_count if f.oos_trade_count else 0
        train_p = f"{f.train_start}~{f.train_end}"
        test_p = f"{f.test_start}~{f.test_end}"
        print(f"  {f.fold_idx+1:<6} {train_p:<26} {test_p:<26} {is_s:>10.4f} {oos_s:>11.4f} {oos_r:>+11.2f}% {oos_t:>8}")

    print("\n  📊 OOS 聚合表现:")
    if result.oos_aggregated:
        m = result.oos_aggregated
        print(f"    平均 Sharpe:    {m.sharpe_ratio:.4f} (±{result.oos_sharpe_std:.4f})")
        print(f"    平均收益:      {m.total_return_pct:+.2f}% (±{result.oos_return_std_pct:.2f}%)")
        print(f"    平均最大回撤:  {m.max_drawdown_pct:.2f}%")
        print(f"    平均胜率:      {m.win_rate:.2f}%")
        print(f"    总交易次数:    {m.trade_count}")

    print("\n  🏆 综合最优参数（按 OOS Sharpe 加权投票）:")
    for k, v in result.best_params_overall.items():
        print(f"    • {k}: {v}")

    # 用最优参数跑全样本回测
    if result.best_params_overall:
        print(f"\n{'='*70}")
        print("  📊 用最优参数跑全样本回测")
        print(f"{'='*70}")
        nested = _flatten_to_nested(result.best_params_overall)
        full_params = {**DEFAULT_BACKTEST_PARAMS, **nested}
        gen = StockAgentTunedV3Signals(market_mode="defend", params=full_params)
        signals = gen.generate_signals(kline_data)
        print(f"  信号数: {len(signals)} (买 {sum(1 for s in signals if s.action=='buy')} / 卖 {sum(1 for s in signals if s.action=='sell')})")

        bt = BacktestEngine(initial_cash=CASH)
        result_full = bt.run(signals, kline_data, benchmark_kline=benchmark_kline)
        print_metrics(result_full.metrics, title="全样本回测指标（最优参数）")

    # 保存 JSON
    out_dir = PROJECT_ROOT / "data" / "grid_search"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"wf_real_holdings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    import json
    output = {
        "stocks": STOCKS,
        "start": START,
        "end": END,
        "grid": GRID,
        "n_folds": result.n_folds,
        "search_seconds": result.search_seconds,
        "oos_sharpe_mean": round(result.oos_sharpe_mean, 4),
        "oos_return_mean_pct": round(result.oos_return_mean_pct, 4),
        "best_params_overall": result.best_params_overall,
        "folds": [
            {
                "fold": f.fold_idx + 1,
                "train": f"{f.train_start}~{f.train_end}",
                "test": f"{f.test_start}~{f.test_end}",
                "is_sharpe": f.is_metrics.sharpe_ratio if f.is_metrics else 0,
                "oos_sharpe": f.oos_metrics.sharpe_ratio if f.oos_metrics else 0,
                "oos_return_pct": f.oos_metrics.total_return_pct if f.oos_metrics else 0,
                "oos_trades": f.oos_trade_count,
                "best_params": f.best_params,
            }
            for f in result.folds
        ],
    }
    with open(out_file, "w", encoding="utf-8") as fp:
        json.dump(output, fp, ensure_ascii=False, indent=2, default=str)
    print(f"\n💾 结果已保存: {out_file}")


if __name__ == "__main__":
    main()
