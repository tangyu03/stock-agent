"""
统一分析引擎
不区分持仓/自选，进场/出场全量扫同一个 stocks 列表
"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from ..config_models import load_config
from ..analyzers.timing_engine import get_timing_engine, EntrySignal, ExitSignal
from ..analyzers.stock_filter import get_stock_filter

logger = logging.getLogger(__name__)


@dataclass
class UnifiedSignalBatch:
    """统一信号批次"""
    market_mode: str = "defend"
    market_score: float = 5.0
    position_limit: float = 0.5
    sector_result: Optional[Dict] = None

    # 板块分类结果（供 engine.py 构建观察列表用）
    stock_sector: Dict[str, str] = field(default_factory=dict)        # code → 板块名称
    stock_sector_status: Dict[str, str] = field(default_factory=dict)  # code → main_trend/rotational/retreating

    # 信号
    entries: List[EntrySignal] = field(default_factory=list)
    exits: List[ExitSignal] = field(default_factory=list)


def _build_sector_for_stock(code: str, sector_map: Dict[str, str],
                              stock_sector_map: Dict[str, str],
                              stock_sector_status: Dict[str, str] = None) -> str:
    """获取个股的板块状态

    优先级：
    1. sector_map（来自 sector_scanner，有明确的板块→状态映射）
    2. stock_sector_status（来自 sector_ranker，用板块涨跌幅排名判定）
    3. "unknown"（都查不到时）

    不再返回 "rotational" 兜底 — 未查到≠轮动。
    """
    # 1. 优先用 sector_map
    sector = stock_sector_map.get(code, "")
    if sector and sector in sector_map:
        return sector_map[sector]
    for full_key in stock_sector_map.get(code, "").split("|"):
        if full_key in sector_map:
            return sector_map[full_key]

    # 2. 用 sector_ranker 的结果
    if stock_sector_status and code in stock_sector_status:
        return stock_sector_status[code]

    # 3. 都查不到
    return "unknown"


def run_unified_analysis(
    data_mode: str = "daily",
    market_mode: str = "defend",
    sector_result=None,
    sector_map: Dict[str, str] = None,
) -> UnifiedSignalBatch:
    """
    统一分析入口。

    Args:
        data_mode: "daily" | "realtime"
        market_mode: 当前市场模式
        sector_result: 板块扫描结果
        sector_map: 板块名 -> 状态映射
    """
    logger.info("====== 统一分析 %s 模式 ======", data_mode)

    batch = UnifiedSignalBatch(
        market_mode=market_mode,
        sector_result=sector_result,
    )

    portfolio = load_config("portfolio.yaml")
    stocks = portfolio.get("stocks") or []
    sector_map = sector_map or {}
    all_codes = [s.get("code", "") for s in stocks if s.get("code")]

    # sector_ranker 统一提供：板块分类 + 行业名 + 概念名（不依赖单独的 SW/概念反查索引）
    stock_sector: Dict[str, str] = {}           # code → 板块名称
    stock_sector_status: Dict[str, str] = {}    # code → main_trend/rotational/retreating/unknown
    ranker_result: Dict[str, dict] = {}         # code → {classification, sectors, best_sector}
    try:
        from ..analyzers.sector_ranker import classify_stocks
        ranker_result = classify_stocks(all_codes)
        for code, info in ranker_result.items():
            stock_sector_status[code] = info.get("classification", "unknown")
            sectors = info.get("sectors", [])
            if sectors:
                best_sector = max(sectors, key=lambda x: abs(x.get("change_pct", 0)))
                stock_sector[code] = best_sector.get("name", "")
        logger.info("sector_ranker 板块状态: %s",
                     {c: v for c, v in stock_sector_status.items() if v != "unknown"})
    except Exception as e:
        logger.warning("sector_ranker 板块分类失败: %s", e)

    # 引擎
    timing = get_timing_engine()
    stock_filter = get_stock_filter()

    # 重置缓存 + 预取大盘数据
    timing.reset_caches()
    timing.prefetch_market_data()

    # 预拉行情
    all_codes = [s.get("code", "") for s in stocks if s.get("code")]
    if all_codes:
        timing.prefetch_hist_batch(all_codes)

    entry_codes = set()

    # -------- 1. 进场信号（全量扫）--------
    logger.info("--- 统一引擎：进场检查 ---")
    for s in stocks:
        code = s.get("code", "")
        name = s.get("name", code)
        if not code:
            continue
        sector = _build_sector_for_stock(code, sector_map, stock_sector, stock_sector_status)

        # 过滤检查（结果传入 check_entry_signals，避免重复调用）
        filter_result = stock_filter.filter_stock(code, name)
        if not filter_result.passed:
            continue

        signals = timing.check_entry_signals(
            stock_code=code, stock_name=name,
            market_mode=market_mode, sector_status=sector,
            filter_result=filter_result,
        )
        if signals:
            batch.entries.extend(signals)
            entry_codes.add(code)

    # -------- 2. 出场信号（全量扫）--------
    logger.info("--- 统一引擎：出场检查 ---")
    for s in stocks:
        code = s.get("code", "")
        name = s.get("name", code)
        if not code:
            continue
        sector = _build_sector_for_stock(code, sector_map, stock_sector, stock_sector_status)
        stock_sector_name = stock_sector.get(code, "")
        exit_sigs = timing.check_exit_signals(
            stock_code=code, stock_name=name,
            market_mode=market_mode, sector_status=sector,
            sector_name=stock_sector_name,
        )
        batch.exits.extend(exit_sigs)

    # -------- 3. 注入板块/概念信息到信号 --------
    # 复用 sector_ranker 已有数据（stock_sector + ranker_result），不触发额外 API 调用
    def _get_sector_info(code: str) -> Dict:
        info = {"sector_name": "", "sw_level2": "", "concepts": ""}
        info["sector_name"] = stock_sector.get(code, "")
        info["sw_level2"] = stock_sector.get(code, "")
        # 概念名从 ranker 的 sectors 中提取（type="概念"）
        rd = ranker_result.get(code, {})
        rd_sectors = rd.get("sectors", [])
        concepts = [s["name"] for s in rd_sectors if s.get("type") == "概念"]
        info["concepts"] = ",".join(concepts[:3])
        return info

    for sig in batch.entries:
        info = _get_sector_info(sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]
        if not getattr(sig, "sw_level2", "") and info["sw_level2"]:
            sig.sw_level2 = info["sw_level2"]
        if not getattr(sig, "concepts", "") and info["concepts"]:
            sig.concepts = info["concepts"]

    for sig in batch.exits:
        if isinstance(sig, dict):
            continue
        info = _get_sector_info(sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]
        if not getattr(sig, "concepts", "") and info["concepts"]:
            sig.concepts = info["concepts"]

    logger.info("统一引擎完成: 进场=%d 出场=%d",
                len(batch.entries), len(batch.exits))

    # 把板块分类结果存到 batch 上，供 engine.py 构建观察列表用
    batch.stock_sector = stock_sector
    batch.stock_sector_status = stock_sector_status

    return batch