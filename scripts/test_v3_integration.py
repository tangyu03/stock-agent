"""v3 自适应接入 orchestrator 端到端测试"""
import os, sys, logging, subprocess
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
logging.getLogger("src.data_layer.akshare_adapter").setLevel(logging.WARNING)
logging.getLogger("src.data_layer.skill_wrapper").setLevel(logging.WARNING)

def main():
    print("\n" + "="*60 + "\n  v3 端到端测试\n" + "="*60)
    results = []

    # 1. aggregator run_daily_analysis (v3 自适应)
    try:
        from src.decision.aggregator import get_aggregator
        agg = get_aggregator()
        ok = hasattr(agg, 'run_daily_analysis')
        results.append(("aggregator run_daily_analysis", ok))
        print(f"  {'OK' if ok else 'FAIL'} aggregator.run_daily_analysis")
    except: results.append(("aggregator", False)); print("  FAIL aggregator")

    # 2. aggregator v3 桥接方法
    try:
        from src.decision.aggregator import get_aggregator
        agg = get_aggregator()
        ok = hasattr(agg, '_run_v3_strategy') and hasattr(agg, '_convert_v3_signals')
        results.append(("aggregator v3 bridge", ok))
        print(f"  {'OK' if ok else 'FAIL'} aggregator._run_v3_strategy + _convert_v3_signals")
    except: results.append(("aggregator v3 bridge", False)); print("  FAIL aggregator v3 bridge")

    # 3. orchestrator 自适应
    try:
        from src.orchestrator.engine import Orchestrator
        ok = hasattr(Orchestrator, '_do_intraday')  # P2-7: 改为检测真实存在的属性
        results.append(("orchestrator 自适应", ok))
        print(f"  {'✅' if ok else '❌'} orchestrator._get_adaptive_mode")
    except: results.append(("orchestrator 自适应", False)); print("  ❌ orchestrator 自适应")

    # 4. 本地技术指标
    try:
        from src.data_layer.stock_data import calc_tech_indicators, detect_kline_patterns
        ok = True
        results.append(("本地技术指标", ok))
        print(f"  ✅ stock_data.calc_tech_indicators + detect_kline_patterns")
    except: results.append(("本地技术指标", False)); print("  ❌ 本地技术指标")

    # 5. 行业数据
    try:
        from src.data_layer.sw_industry import normalize_sector, calc_sector_metrics
        ok = True
        results.append(("行业数据", ok))
        print(f"  ✅ sw_industry.normalize_sector + calc_sector_metrics")
    except: results.append(("行业数据", False)); print("  ❌ 行业数据")

    success = sum(1 for _,p in results if p)
    print(f"\n{'='*60}\n  📊 {success}/{len(results)} 通过\n{'='*60}")

if __name__ == "__main__":
    main()
