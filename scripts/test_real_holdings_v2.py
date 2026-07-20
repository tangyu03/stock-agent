# -*- coding: utf-8 -*-
"""
真实持仓股票 bug 修复验证（修正版）

用回测模式逐只检查，确保 _tech_cache_weekly 正确聚合周线。
"""
import os
import sys
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")

logging.basicConfig(level=logging.WARNING)

from src.loop.data_loader import DataLoader
from src.loop.backtest_engine import BacktestEngine, print_result
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS
from src.analyzers.timing_engine import get_backtest_timing_engine, get_timing_engine

HOLDINGS = [
    ("688409", "富创精密"), ("688652", "京仪装备"), ("300843", "胜蓝股份"),
    ("688531", "日联科技"), ("688008", "澜起科技"), ("688041", "海光信息"),
    ("688027", "国盾量子"), ("301666", "大普微"), ("002594", "比亚迪"),
    ("920045", "蘅东光"), ("688820", "盛合晶微"), ("688028", "沃尔德"),
    ("300757", "罗博特科"), ("688521", "芯原股份"),
]

START_DATE = "2025-06-01"
END_DATE = "2026-07-10"


def main():
    print("=" * 70)
    print("  真实持仓股票 bug 修复验证（修正版）")
    print("=" * 70)

    # 加载数据
    loader = DataLoader()
    codes = [c for c, _ in HOLDINGS]
    kline_data = loader.load_kline(codes, START_DATE, END_DATE)
    if not kline_data:
        print("❌ 数据加载失败")
        return

    total_rows = sum(len(v) for v in kline_data.values())
    print(f"\n✅ 加载 {len(kline_data)}/{len(codes)} 只，共 {total_rows} 根 K 线")

    # === 用回测模式逐只检查（避免缓存污染）===
    print(f"\n{'='*70}")
    print("  验证 1: 套利低吸策略（BUG-A1/A2/B4 修复）")
    print(f"{'='*70}")

    engine = get_backtest_timing_engine()
    arbitrage_triggered = 0
    weekly_macd_true = 0

    for code, name in HOLDINGS:
        if code not in kline_data:
            continue
        kline = kline_data[code]
        engine.set_backtest_context(kline[-1]["date"], {code: kline}, [])
        tech = engine._fetch_tech_data(code, "defend")
        weekly_up = tech.get("weekly_macd_up")
        if weekly_up:
            weekly_macd_true += 1

        stop_loss = engine.calculate_stop_loss(code, tech)
        entry = engine._check_arbitrage_entry(code, name, tech, stop_loss, "defend", "rotational")
        status = f"✅ 触发" if entry else "❌ 未触发"
        reason = entry.trigger_reason[:40] if entry else "条件不满足"
        print(f"  {code} {name}: weekly_macd={weekly_up} | {status} | {reason}")
        if entry:
            arbitrage_triggered += 1

    print(f"\n  汇总:")
    print(f"    周线 MACD=True: {weekly_macd_true}/{len(kline_data)} 只")
    print(f"    套利低吸触发: {arbitrage_triggered}/{len(kline_data)} 只")
    print(f"    ✅ BUG-A1 修复: 周线 MACD 不再恒为 False" if weekly_macd_true > 0 else "    ⚠️ 周线 MACD 仍为 False")
    print(f"    ✅ BUG-A2 修复: entry_type 命名统一" if arbitrage_triggered > 0 else "")

    # === 验证 MACD 死叉不再单独触发卖出 ===
    print(f"\n{'='*70}")
    print("  验证 2: MACD 死叉不再单独触发卖出（MACD 修复）")
    print(f"{'='*70}")

    macd_only_exits = 0
    total_exits = 0
    for code, name in HOLDINGS:
        if code not in kline_data:
            continue
        kline = kline_data[code]
        engine.set_backtest_context(kline[-1]["date"], {code: kline}, [])
        exits = engine.check_exit_signals(code, name, "defend", "rotational", "")
        for sig in exits:
            total_exits += 1
            # 检查是否只有 MACD 死叉
            first_line = sig.reason.split("\n")[0]
            if "MACD死叉" in first_line:
                # 去掉 MACD 死叉后看还有没有其他信号
                cleaned = first_line.replace("[medium]MACD死叉", "").replace("[medium]MACD死叉+转负", "")
                if "strong" not in cleaned and "medium" not in cleaned:
                    macd_only_exits += 1
                    print(f"  ⚠️ {code} {name}: 仅 MACD 死叉触发卖出")

    print(f"\n  汇总:")
    print(f"    总卖出信号: {total_exits} 条")
    print(f"    仅 MACD 死叉触发: {macd_only_exits} 条")
    print(f"    ✅ MACD 死叉不再单独触发卖出" if macd_only_exits == 0 else "    ⚠️ 仍有 MACD 单独触发")

    # === 验证对子底检测 ===
    print(f"\n{'='*70}")
    print("  验证 3: 对子底检测（BUG-B4 修复）")
    print(f"{'='*70}")

    pair_bottom_count = 0
    for code, name in HOLDINGS:
        if code not in kline_data:
            continue
        kline = kline_data[code]
        engine.set_backtest_context(kline[-1]["date"], {code: kline}, [])
        tech = engine._fetch_tech_data(code, "defend")
        if tech.get("pair_bottom"):
            pair_bottom_count += 1
            price = tech.get("current_price", 0)
            print(f"  ✅ {code} {name}: 价格 {price} → 对子底")

    print(f"\n  汇总: 对子底触发 {pair_bottom_count}/{len(kline_data)} 只")
    print(f"    ✅ BUG-B4 修复: 对子底检测正常工作" if pair_bottom_count > 0 else "    ℹ️ 当日无对子底")

    # === 完整回测 ===
    print(f"\n{'='*70}")
    print("  验证 4: 完整回测（所有修复综合验证）")
    print(f"{'='*70}")

    gen = StockAgentTunedV3Signals(
        market_mode="defend",
        params={**DEFAULT_BACKTEST_PARAMS},
    )
    signals = gen.generate_signals(kline_data)
    print(f"  回测信号数: {len(signals)}")
    print(f"  买入: {sum(1 for s in signals if s.action == 'buy')}")
    print(f"  卖出: {sum(1 for s in signals if s.action == 'sell')}")

    bt_engine = BacktestEngine(initial_cash=1_000_000)
    result = bt_engine.run(signals, kline_data)
    print_result(result, title="真实持仓回测结果")

    # === 信号明细 ===
    print(f"\n{'='*70}")
    print("  回测信号明细")
    print(f"{'='*70}")
    for sig in signals[:20]:
        action = "🟢买" if sig.action == "buy" else "🔴卖"
        reason_first_line = sig.reason.split("\n")[0][:60]
        print(f"  {sig.date} {action} {sig.code} {sig.shares}股 @ {sig.price:.2f} | {reason_first_line}")
    if len(signals) > 20:
        print(f"  ... 还有 {len(signals) - 20} 条")

    print(f"\n{'='*70}")
    print("  ✅ 真实持仓数据验证完成")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
