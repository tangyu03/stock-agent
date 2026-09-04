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
    entry_diagnostics: Dict[str, str] = field(default_factory=dict)   # code → 未触发买入的原因

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


def _explain_no_entry(
    market_mode: str,
    sector_status: str,
    tech_data: Dict,
) -> str:
    """生成不触发买入的高层门槛说明，供观察列表展示。"""
    mode_names = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}
    sector_names = {
        "main_trend": "主线",
        "rotational": "轮动",
        "retreating": "退潮",
        "unknown": "未知",
    }
    if tech_data.get("entry_blocked_reason"):
        primary = tech_data["entry_blocked_reason"]
    tech = tech_data.get("tech_signals") or {}
    tech_score = float(tech.get("vote_score", 0) or 0)
    institutional = tech_data.get("institutional_holding") or {}
    inst_score = float(institutional.get("vote_score", 0) or 0)
    strategy_reasons = _strategy_blockers(market_mode, sector_status, tech_data)

    if sector_status == "retreating":
        primary = "板块退潮，禁止新入场"
    elif market_mode == "retreat":
        primary = "撤退模式只允许恐慌抄底"
    elif tech_score < 0:
        primary = "技术投票偏空，未触发买入"
    elif tech_score == 0:
        primary = "技术投票中性，未触发买入"
    else:
        primary = "技术偏多但四种入场策略均未达到触发阈值"

    mode = mode_names.get(market_mode, market_mode)
    sector = sector_names.get(sector_status, sector_status)
    return (
        f"{primary}\n"
        f"模式:{mode} | 板块:{sector}\n"
        "策略检查:\n"
        + "\n".join(f"- {reason}" for reason in strategy_reasons)
        + f"\n评分: 技术 {tech_score:+.1f} | 机构 {inst_score:+.0f}"
    )


def _strategy_blockers(
    market_mode: str,
    sector_status: str,
    tech_data: Dict,
) -> List[str]:
    """List the entry gate that failed for each of the four strategies."""
    if sector_status == "retreating":
        return ["全部策略: 板块退潮，禁止新买入"]

    blockers: List[str] = []
    panic_reason = _panic_bottom_blocker(tech_data)
    if panic_reason:
        blockers.append(f"恐慌抄底: {panic_reason}")

    if market_mode not in ("attack", "defend"):
        blockers.append("套利低吸: 撤退模式禁用")
    elif not tech_data.get("weekly_macd_up"):
        blockers.append("套利低吸: 周线MACD未向上")
    elif not any(
        tech_data.get(key)
        for key in (
            "shrinking_pullback_ma5",
            "shrinking_pullback_ma10",
            "pair_bottom",
            "daily_limit_opened",
        )
    ):
        blockers.append("套利低吸: 未出现低吸形态")

    if market_mode != "attack":
        blockers.append("确认追强: 仅进攻模式启用")
    else:
        chase_reason = _momentum_chase_blocker(tech_data)
        if chase_reason:
            blockers.append(f"确认追强: {chase_reason}")

    if market_mode not in ("attack", "defend"):
        blockers.append("价量突破: 撤退模式禁用")
    else:
        breakout_reason = _volume_breakout_blocker(tech_data, market_mode)
        if breakout_reason:
            blockers.append(f"价量突破: {breakout_reason}")

    return blockers


def _panic_bottom_blocker(tech_data: Dict) -> str:
    index_drop = abs(float(tech_data.get("index_daily_drop", 0) or 0))
    gem_star_drop = abs(float(tech_data.get("gem_sci_tech_drop", 0) or 0))
    ad_ratio = float(tech_data.get("advance_decline_ratio", 1.0) or 1.0)
    market_panic = index_drop > 4.0 or gem_star_drop > 5.0 or ad_ratio < 0.15
    if not market_panic:
        return "正常行情，未触发"

    tech = tech_data.get("tech_signals") or {}
    oversold = any(
        (
            tech_data.get("change_pct", 0) is not None
            and float(tech_data.get("change_pct", 0) or 0) < -7,
            tech.get("rsi") is not None and float(tech["rsi"]) < 30,
            float(tech_data.get("drop_5d", 0) or 0) < -0.15,
            (tech.get("bollinger") or {}).get("position") == "below",
            tech_data.get("has_hammer"),
            (tech.get("kdj") or {}).get("j", 50) < 0,
        )
    )
    return "缺个股超卖" if not oversold else "已见恐慌/超卖，但确认不足"


def _momentum_chase_blocker(tech_data: Dict) -> str:
    current = float(tech_data.get("current_price", 0) or 0)
    ma20 = float(tech_data.get("ma20", 0) or 0)
    recent_high = float(tech_data.get("recent_high", 0) or 0)
    if not current or not ma20 or not recent_high:
        return "数据不足"

    kline = tech_data.get("kline") or []
    if len(kline) < 21:
        return "K线不足21日"
    closes = [float(k.get("收盘", k.get("close", 0)) or 0) for k in kline]
    yesterday_ma20 = sum(closes[-21:-1]) / 20
    if ma20 <= yesterday_ma20:
        return "MA20未向上"
    if current < recent_high * 0.99:
        return "未接近20日高点"
    if float(tech_data.get("volume_ratio", 1.0) or 1.0) < 1.2:
        return "量比不足"
    return ""


def _volume_breakout_blocker(tech_data: Dict, market_mode: str) -> str:
    current = float(tech_data.get("current_price", 0) or 0)
    ma25 = float(tech_data.get("ma25", 0) or 0)
    kline_count = len(tech_data.get("kline") or [])
    if kline_count < 60:
        return f"K线不足60日(实际{kline_count}条)，暂不判断"
    if not current or not ma25:
        return "行情字段不足"

    if market_mode == "attack":
        rs = tech_data.get("rs_line") or {}
        if rs and rs.get("rs_latest", 0) <= rs.get("rs_ma", float("inf")):
            return "RS弱"
    if current <= ma25:
        return "未站上MA25"
    from ..analyzers.signal_plan import build_volume_snapshot

    volume_snapshot = build_volume_snapshot(tech_data)
    if volume_snapshot.dirty:
        return volume_snapshot.dirty_reason
    if volume_snapshot.volume_vs_ma60 is None:
        return "量能数据不足"
    if (
        (volume_snapshot.volume_vs_ma60 is None or volume_snapshot.volume_vs_ma60 <= 1.0)
        and float(tech_data.get("change_pct", 0) or 0) < 9.5
    ):
        return "量未破60日均量"
    return ""


def run_unified_analysis(
    data_mode: str = "daily",
    market_mode: str = "defend",
    sector_result=None,
    sector_map: Dict[str, str] = None,
    market_score: Optional[float] = None,
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
        market_score=float(market_score or 5.0),
        sector_result=sector_result,
    )

    portfolio = load_config("portfolio.yaml")
    stocks = portfolio.get("stocks") or []
    sector_map = sector_map or {}
    all_codes = [s.get("code", "") for s in stocks if s.get("code")]

    # sector_ranker 统一提供：板块分类 + 行业名（概念模块已移除，分类只按行业）
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
                # sectors 只含行业类型（东财行业/新浪行业/同花顺行业/THS行业-计算/默认兜底），
                # 取涨跌幅绝对值最大的作为"板块"名。
                industry_types = ("东财行业", "行业", "同花顺行业", "THS行业-计算")
                industry_pool = [s for s in sectors if s.get("type") in industry_types]
                pool = industry_pool or sectors
                best_sector = max(pool, key=lambda x: abs(x.get("change_pct", 0)))
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
            batch.entry_diagnostics[code] = "风控过滤: " + "; ".join(filter_result.failed_checks or ["未通过"])
            continue

        signals = timing.check_entry_signals(
            stock_code=code, stock_name=name,
            market_mode=market_mode, sector_status=sector,
            filter_result=filter_result,
            market_score=market_score,
        )
        if signals:
            batch.entries.extend(signals)
            entry_codes.add(code)
        else:
            entry_tech = timing._tech_data_full.get(code, {})
            batch.entry_diagnostics[code] = _explain_no_entry(
                market_mode, sector, entry_tech if isinstance(entry_tech, dict) else {}
            )

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

    # -------- 3. 注入板块信息到信号 --------
    # 复用 sector_ranker 已有数据（stock_sector + ranker_result），不触发额外 API 调用
    def _get_sector_info(code: str) -> Dict:
        info = {"sector_name": "", "sw_level2": ""}
        info["sector_name"] = stock_sector.get(code, "")
        info["sw_level2"] = stock_sector.get(code, "")
        return info

    for sig in batch.entries:
        info = _get_sector_info(sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]
        if not getattr(sig, "sw_level2", "") and info["sw_level2"]:
            sig.sw_level2 = info["sw_level2"]

    for sig in batch.exits:
        if isinstance(sig, dict):
            continue
        info = _get_sector_info(sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]

    logger.info("统一引擎完成: 进场=%d 出场=%d",
                len(batch.entries), len(batch.exits))

    # 把板块分类结果存到 batch 上，供 engine.py 构建观察列表用
    batch.stock_sector = stock_sector
    batch.stock_sector_status = stock_sector_status

    return batch
