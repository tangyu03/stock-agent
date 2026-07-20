# -*- coding: utf-8 -*-
"""
基于信号质量的网格搜索（事件研究法）

不模拟交易，只评估信号本身的预测能力。
与持仓/成本/股数完全无关，与实盘逻辑完全一致。

优化目标：综合评分 = 买入期望值×0.6 + 卖出期望值×0.4
"""
import os
import sys
import logging
import json
import itertools
from pathlib import Path
from datetime import datetime
from collections import Counter

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

from src.loop.data_loader import DataLoader
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS
from src.loop.signal_evaluator import SignalEvaluator, print_signal_metrics

# 实际持仓
STOCKS = [
    "688409", "688652", "300843", "688531", "688008", "688041",
    "688027", "301666", "002594", "920045", "688820", "688028",
    "300757", "688521",
]
START = "2025-06-01"
END = "2026-07-10"

# 搜索空间（TimingEngine 阈值参数）
GRID = {
    "exit.exhaustion.rsi_overbought": [65, 70, 75],
    "exit.exhaustion.strong_signal_min_count": [1, 2, 3],
    "exit.exhaustion.ma5_bias_overheat": [0.06, 0.08, 0.10],
    "stop_loss.multiplier": [0.95, 0.97],
}


def _flatten_to_nested(flat):
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
    print("  基于信号质量的网格搜索（事件研究法）")
    print(f"  股票: {len(STOCKS)} 只, 时间: {START} ~ {END}")
    total_combos = 1
    for v in GRID.values():
        total_combos *= len(v)
    print(f"  搜索空间: {len(GRID)} 维, {total_combos} 组合")
    print(f"  评估窗口: 买入后 5 天收益")
    print(f"  优化目标: 综合评分 = 买入期望×0.6 + 卖出期望×0.4")
    print("=" * 70)

    # 加载数据
    print("\n📥 加载数据...")
    loader = DataLoader()
    kline_data = loader.load_kline(STOCKS, START, END)
    if not kline_data:
        print("❌ 数据加载失败")
        return
    print(f"   ✅ {len(kline_data)} 只, {sum(len(v) for v in kline_data.values())} 根 K 线")

    # 评估器
    evaluator = SignalEvaluator(hold_days=5)

    # 网格搜索
    keys = list(GRID.keys())
    value_lists = [GRID[k] for k in keys]
    all_combos = list(itertools.product(*value_lists))

    print(f"\n🔍 开始搜索 {len(all_combos)} 组合...")
    results = []
    best_score = -999
    best_combo = None
    start_time = datetime.now()

    for i, combo in enumerate(all_combos, 1):
        flat_params = dict(zip(keys, combo))
        nested = _flatten_to_nested(flat_params)
        full_params = {**DEFAULT_BACKTEST_PARAMS, **nested}

        # 生成信号
        gen = StockAgentTunedV3Signals(market_mode="defend", params=full_params)
        signals = gen.generate_signals(kline_data)

        # 评估信号质量
        m = evaluator.evaluate(signals, kline_data)
        m_dict = m.to_dict()
        m_dict["params"] = flat_params
        results.append(m_dict)

        if m.total_score > best_score:
            best_score = m.total_score
            best_combo = m_dict

        if i % 9 == 0 or i == len(all_combos):
            elapsed = (datetime.now() - start_time).total_seconds()
            print(f"  进度 {i}/{len(all_combos)} ({i/len(all_combos)*100:.0f}%) "
                  f"已用 {elapsed:.0f}s 当前最优 score={best_score:+.4f}")

    # 排序输出 Top 10
    results.sort(key=lambda x: x["total_score"], reverse=True)

    print(f"\n{'='*90}")
    print(f"  🏆 Top 10 参数组合（按综合评分排序）")
    print(f"{'='*90}")
    print(f"  {'排名':<4} {'score':>8} {'买入胜率%':>8} {'买入期望':>8} {'卖出避免%':>8} {'卖出期望':>8} "
          f"{'RSI超买':>6} {'strong':>6} {'MA5过热':>6} {'止损×':>5}")
    print("  " + "-" * 88)
    for rank, r in enumerate(results[:10], 1):
        p = r["params"]
        print(f"  {rank:<4} {r['total_score']:>+8.4f} {r['buy_win_rate']:>8.2f} "
              f"{r['buy_expectancy']:>+8.4f} {r['sell_avoid_loss_rate']:>8.2f} "
              f"{r['sell_expectancy']:>+8.4f} "
              f"{p.get('exit.exhaustion.rsi_overbought',''):>6} "
              f"{p.get('exit.exhaustion.strong_signal_min_count',''):>6} "
              f"{p.get('exit.exhaustion.ma5_bias_overheat',''):>6} "
              f"{p.get('stop_loss.multiplier',''):>5}")

    # 最优参数详细评估
    print(f"\n{'='*70}")
    print(f"  🏆 最优参数详细评估")
    print(f"{'='*70}")
    if best_combo:
        print(f"  参数:")
        for k, v in best_combo["params"].items():
            print(f"    • {k}: {v}")

        # 用最优参数重新生成信号并打印详细指标
        nested = _flatten_to_nested(best_combo["params"])
        full_params = {**DEFAULT_BACKTEST_PARAMS, **nested}
        gen = StockAgentTunedV3Signals(market_mode="defend", params=full_params)
        signals = gen.generate_signals(kline_data)

        print(f"\n  信号统计:")
        print(f"    总信号数: {len(signals)}")
        print(f"    买入: {sum(1 for s in signals if s.action=='buy')}")
        print(f"    卖出: {sum(1 for s in signals if s.action=='sell')}")

        m = evaluator.evaluate(signals, kline_data)
        print_signal_metrics(m, title="最优参数信号质量")

    # 对比默认参数
    print(f"\n{'='*70}")
    print(f"  📊 默认参数 vs 最优参数对比")
    print(f"{'='*70}")
    gen_default = StockAgentTunedV3Signals(market_mode="defend", params={**DEFAULT_BACKTEST_PARAMS})
    signals_default = gen_default.generate_signals(kline_data)
    m_default = evaluator.evaluate(signals_default, kline_data)

    print(f"  {'指标':<16} {'默认参数':>12} {'最优参数':>12} {'改善':>12}")
    print(f"  {'-'*56}")
    for label, attr, fmt in [
        ("综合评分", "total_score", "{:+.4f}"),
        ("买入信号数", "buy_signal_count", "{:d}"),
        ("买入胜率%", "buy_win_rate", "{:.2f}%"),
        ("买入期望", "buy_expectancy", "{:+.4f}"),
        ("卖出信号数", "sell_signal_count", "{:d}"),
        ("卖出避免率%", "sell_avoid_loss_rate", "{:.2f}%"),
        ("卖出期望", "sell_expectancy", "{:+.4f}"),
    ]:
        dv = fmt.format(getattr(m_default, attr))
        bv = fmt.format(getattr(best_combo, attr) if hasattr(best_combo, attr) else best_combo[attr]) if best_combo else "—"
        print(f"  {label:<16} {dv:>12} {bv:>12}")

    # 保存 JSON
    out_dir = PROJECT_ROOT / "data" / "signal_search"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"signal_search_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output = {
        "stocks": STOCKS,
        "start": START,
        "end": END,
        "grid": GRID,
        "hold_days": 5,
        "search_time": datetime.now().isoformat(),
        "best_params": best_combo["params"] if best_combo else None,
        "best_score": best_score,
        "top_10": results[:10],
        "all_results": results,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n💾 结果已保存: {out_file}")

    print(f"\n{'='*70}")
    print(f"  ✅ 网格搜索完成！")
    print(f"  📌 将最优参数写入 config/timing.yaml 即可让实盘生效")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
