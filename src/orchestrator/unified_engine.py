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

    # 【三】信号事件状态迁移通知（失效撤单/过期）
    event_notices: List[Dict] = field(default_factory=list)
    # 【二】主题归属修正记录（行业数据库映射 ≠ 市场主题交易，Phase2-B）
    theme_remaps: List[Dict] = field(default_factory=list)

    # 【Phase4 P1-2】再入场待命队列（创新高/收复Z线的头部标的）
    standby_queue: List[Dict] = field(default_factory=list)
    # 【Phase4 P1-1】防守模式追强踏空成本台账（回退禁用后继续留档）
    # 【Phase4 P2-1】板块分类版本
    theme_version: str = ""
    # F2-2 审计：板块状态/归属相对前一报告日的变化
    sector_changes: Optional[Dict] = None


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

    if market_mode == "retreat":
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
    """List the entry gate that failed for each of the five strategies."""
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
        blockers.append("确认追强: 防守模式禁用")
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

    # 【P3-1】趋势延续：第五策略（回踩型，防守模式可用）
    if market_mode not in ("attack", "defend"):
        blockers.append("趋势延续: 撤退模式禁用")
    else:
        cont_reason = _trend_continuation_blocker(tech_data)
        if cont_reason:
            blockers.append(f"趋势延续: {cont_reason}")

    return blockers



def _panic_bottom_blocker(tech_data: Dict) -> str:
    from ..decision.mode_rules import evaluate_market_panic
    market_panic = evaluate_market_panic(
        index_drop=tech_data.get("index_daily_drop"),
        gem_star_drop=tech_data.get("gem_sci_tech_drop"),
        ad_ratio=tech_data.get("advance_decline_ratio"),
        cum_drop_5d=tech_data.get("index_cum_drop_5d"),
        down_days_5d=tech_data.get("index_down_days_5d"),
    )["triggered"]
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
    change_pct = float(tech_data.get("change_pct", 0) or 0)
    projected = volume_snapshot.projected_volume_vs_ma60
    projected_ok = (
        projected is not None
        and projected >= 1.2
        and volume_snapshot.projection_mode == "ok"
    )
    if (
        (volume_snapshot.volume_vs_ma60 is None or volume_snapshot.volume_vs_ma60 <= 1.0)
        and change_pct < 9.5
        and not projected_ok
    ):
        # 【P0-1】外推口径披露：累计量未达标时展示外推判断
        if projected is not None and volume_snapshot.projection_mode == "ok":
            return f"量未破60日均量(累计{volume_snapshot.volume_vs_ma60:.2f}x，外推{projected:.2f}x)"
        return "量未破60日均量"
    return ""


def _trend_continuation_blocker(tech_data: Dict) -> str:
    """【P3-1】趋势延续策略的拦截原因（观察卡展示）。"""
    ma5 = float(tech_data.get("ma5", 0) or 0)
    ma10 = float(tech_data.get("ma10", 0) or 0)
    ma20 = float(tech_data.get("ma20", 0) or 0)
    ma25 = float(tech_data.get("ma25", 0) or 0)
    current = float(tech_data.get("current_price", 0) or 0)
    prev_close = float(tech_data.get("prev_close", 0) or 0)
    ma25_prev = tech_data.get("ma25_prev")
    vol_ratio = float(tech_data.get("volume_ratio", 1.0) or 1.0)

    if not all([ma5, ma10, ma20, ma25, current]):
        return "行情字段不足"
    if not (ma5 > ma10 > ma20):
        return "非MA多头排列"
    if current <= ma25:
        return "未站上MA25"
    if not (prev_close and ma25_prev and prev_close > float(ma25_prev)):
        return "非延续段(突破当日归价量突破)"
    if current < ma10:
        return "回踩破MA10"
    if vol_ratio < 1.2:
        return f"再度放量不足(量比{vol_ratio:.1f})"
    return ""


def run_unified_analysis(
    data_mode: str = "daily",
    market_mode: str = "defend",
    sector_result=None,
    sector_map: Dict[str, str] = None,
    market_score: Optional[float] = None,
    ref_date: str = "",
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

    # F2-2 审计：先留存板块级当日状态，主题映射后再比对个股归属。
    sector_states: Dict[str, str] = {}
    for info in ranker_result.values():
        for sector in info.get("sectors", []):
            name = str(sector.get("name") or "")
            classification = str(sector.get("classification") or "")
            if name and classification:
                sector_states[name] = classification

    # 【二】主题归属修正（Phase2-B）：行业数据库自动映射把澜起/海光/大普微/
    # 中科飞测/芯碁微装/胜蓝/兆易创新误归“电子化学品”，骄成超声误归“电池”，
    # 创世纪误归“自动化设备”——而板块状态直接决定“禁追强/低吸可用”闸门。
    # 修正规则：人工主题映射优先、行业链路兜底；状态取主题代理板块（THS 行业）
    # 的最严格状态（retreating > main_trend > rotational），代理状态不可得时
    # 沿用原行业状态。修正记录透出到 batch.theme_remaps（调度摘要可审计）。
    try:
        from ..analyzers.theme_attribution import apply_theme_attribution, make_board_status_lookup, get_theme_version
        name_by_code = {s.get("code", ""): s.get("name", "") for s in stocks}
        board_status_fn = make_board_status_lookup(sector_map)
        theme_report = apply_theme_attribution(
            stock_sector, stock_sector_status, name_by_code, board_status_fn,
        )
        batch.theme_remaps = theme_report.get("remaps", [])
        # 【P2-1】板块版本化：分类口径打版本戳，历史统计可按版本切分
        batch.theme_version = get_theme_version()
    except Exception as e:
        logger.warning("主题归属修正失败（沿用原行业链路）: %s", e)

    try:
        from ..feedback.sector_changes import record_sector_changes
        name_by_code = {s.get("code", ""): s.get("name", "") for s in stocks}
        batch.sector_changes = record_sector_changes(
            ref_date or __import__("datetime").date.today().isoformat(),
            sector_states,
            stock_sector,
            stock_sector_status,
            name_by_code=name_by_code,
        )
    except Exception as e:
        logger.warning("板块变更快照失败: %s", e)

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
    # 【P1-1】防守模式追强踏空成本台账（数据本身是“防守模式是否值得存在”的证据）

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
            entry_tech = entry_tech if isinstance(entry_tech, dict) else {}
            diagnostics = _explain_no_entry(
                market_mode, sector, entry_tech,
            )
            # 【三】观察卡补充活跃事件状态（回踩买点有效/第几天）
            event_note = timing.lifecycle_status_note(
                code, float((entry_tech or {}).get("current_price") or 0)
            )
            if event_note:
                diagnostics += f"\n事件状态: {event_note}"
            # 【P1-2】再入场待命判定：止损后的标的不该掉回观察区中部，
            # 创新高/收复Z线的进待命队列头部（蘅东光 9/7 应在头部）
            # 【Phase5 回炉】创新高锄 prior_high（剔除当日高点）——
            # 蘅东光 9/7 盘中 555.10 创历史新高、收盘 549.50 回落 1%，
            # 旧口径（含当日）差 0.05 元误拦头部待命。
            try:
                active_events = timing._lifecycle.get_active_events(code)
            except Exception:
                active_events = []
            active_event_id = active_events[-1].event_id if active_events else ""
            try:
                from ..analyzers.signal_lifecycle import reentry_status
                current_price = float((entry_tech or {}).get("current_price") or 0)
                recent_high = float((entry_tech or {}).get("recent_high") or 0)
                prior_high = float((entry_tech or {}).get("prior_high") or 0)
                reentry_cfg = timing._cfg("reentry") or {}
                status = reentry_status(
                    code, current_price=current_price, recent_high=recent_high,
                    new_high_ratio=float(reentry_cfg.get("new_high_ratio", 0.99)),
                    store=timing._lifecycle.store,
                    prior_high=prior_high,
                )
                if status.get("standby"):
                    batch.standby_queue.append({
                        "stock_code": code, "stock_name": name,
                        "event_id": active_event_id or status.get("event_id", ""),
                        "reason": status.get("reason"),
                        "note": status.get("note"),
                        "attempts": status.get("attempts", 0),
                    })
                    diagnostics = status["note"] + "\n" + diagnostics
            except Exception as e:
                logger.debug("再入场判定失败 %s: %s", code, str(e)[:60])
            # 【P1-1】防守模式追强被拦 → 踏空成本入台账（四确认里至少创新高+放量
            # 才登记：不是每只下跌股都算磨空，只有“本可放行却被纪律拦下”的才算）
            batch.entry_diagnostics[code] = diagnostics

    # -------- 2. 出场信号（全量扫）--------
    logger.info("--- 统一引擎：出场检查 ---")
    frozen_scores = {}
    for s in stocks:
        code = s.get("code", "")
        score = ((timing._tech_data_full.get(code) or {}).get("tech_signals") or {}).get("vote_score")
        if score is not None:
            frozen_scores[code] = float(score)
    try:
        batch.event_notices.extend(
            timing.run_daily_event_unfreeze(frozen_scores)
        )
    except Exception as e:
        logger.debug("日终解冻批处理失败: %s", e)
    for s in stocks:
        code = s.get("code", "")
        name = s.get("name", code)
        if not code:
            continue
        sector = _build_sector_for_stock(code, sector_map, stock_sector, stock_sector_status)
        stock_sector_name = stock_sector.get(code, "")
        sector_change_pct = None
        best_sector = (ranker_result.get(code) or {}).get("best_sector") or {}
        try:
            raw_sector_change = best_sector.get("change_pct")
            if raw_sector_change is not None:
                sector_change_pct = float(raw_sector_change)
        except (TypeError, ValueError):
            sector_change_pct = None
        exit_sigs = timing.check_exit_signals(
            stock_code=code, stock_name=name,
            market_mode=market_mode, sector_status=sector,
            sector_name=stock_sector_name,
            sector_change_pct=sector_change_pct,
        )
        batch.exits.extend(exit_sigs)

        # 【三】信号事件生命周期评估：收盘跌回突破位/板块退潮 → 立即撤单；
        # 超期 → 作废。状态通知只进事件通道，不进卖出桶；
        # “信号成交”不是卖出，“信号作废”也不是卖出指令。
        current_price = float(
            (timing._tech_data_full.get(code) or {}).get("current_price") or 0
        )
        notices = timing.evaluate_signal_events(
            code, current_price=current_price, sector_status=sector
        )
        if notices:
            batch.event_notices.extend(notices)

    # -------- 2b. 事件状态刷新（P0-2 一致性校验）--------
    # 正文诊断的“事件状态:”行在入场循环生成，早于出场循环的 freeze/unfreeze/
    # mark_filled；直接渲染会与头部在飞栏（渲染时点读库最新状态）矛盾
    # （沃尔德 9-18：正文显示“已冻结”而头部已“已成交”）。
    # 此处对仍带事件状态行的诊断按库最新状态重渲染，保证同源同时点。
    try:
        for code, diag in batch.entry_diagnostics.items():
            if not (isinstance(diag, str) and "\n事件状态: " in diag):
                continue
            marker = "\n事件状态: "
            prefix = diag.split(marker, 1)[0]
            price = float(
                (timing._tech_data_full.get(code) or {}).get("current_price") or 0
            )
            note = timing.lifecycle_status_note(code, price)
            if note:
                batch.entry_diagnostics[code] = prefix + marker + note
            else:
                batch.entry_diagnostics[code] = prefix
    except Exception as e:
        logger.debug("事件状态刷新失败: %s", str(e)[:80])
    # -------- 3. 注入板块信息到信号 --------
    # 复用 sector_ranker 已有数据（stock_sector + ranker_result），不触发额外 API 调用
    def _get_sector_info(code: str) -> Dict:
        info = {"sector_name": "", "sw_level2": ""}
        info["sector_name"] = stock_sector.get(code, "")
        info["sw_level2"] = stock_sector.get(code, "")
        return info

    def _attach_sector_regime(signal, code: str) -> None:
        info = ranker_result.get(code) or {}
        sector = info.get("best_sector") or {}
        if not sector and info.get("sectors"):
            sector = (info.get("sectors") or [{}])[0]
        if not getattr(signal, "sector_change_pct", None):
            signal.sector_change_pct = sector.get("change_pct")
        if not getattr(signal, "sector_rank", None):
            signal.sector_rank = sector.get("rank")
        if not getattr(signal, "sector_total", None):
            signal.sector_total = sector.get("total")

    for sig in batch.entries:
        info = _get_sector_info(sig.stock_code)
        _attach_sector_regime(sig, sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]
        if not getattr(sig, "sw_level2", "") and info["sw_level2"]:
            sig.sw_level2 = info["sw_level2"]

    # ============================================================
    # 【P2-3】组合预算：同板块并发敞口上限（个股纪律齐备，集群不能裸喋）
    # 9/7 观察 23/24 主线、半导体设备 8 只同标签——超额同板块新信号
    # 降级为观察并留痕，个股纪律结论保留可审计。
    # ============================================================
    # ============================================================
    # 【P3-2】第八问驱动源归因：每条入场信号带上“为什么涨”
    # （博杰真实驱动=业绩+PCB联动，系统此前只看到价格表象）
    # ============================================================
    for sig in batch.entries:
        try:
            from ..analyzers.driver_attribution import classify_driver
            driver_info = classify_driver(
                (sig.tech_data or {}),
                sector_status=sig.sector_status or "",
                sector_name=sig.sector_name or "",
            )
            sig.driver = driver_info
        except Exception as e:
            logger.debug("驱动源归因失败 %s: %s", sig.stock_code, str(e)[:60])

    for sig in batch.exits:
        if isinstance(sig, dict):
            continue
        info = _get_sector_info(sig.stock_code)
        _attach_sector_regime(sig, sig.stock_code)
        if not getattr(sig, "sector_name", "") and info["sector_name"]:
            sig.sector_name = info["sector_name"]

    logger.info(
        "统一引擎完成: 进场=%d 出场=%d 待命=%d 踏空留档=%d",
        len(batch.entries), len(batch.exits),
        len(batch.standby_queue),
    )

    # 把板块分类结果存到 batch 上，供 engine.py 构建观察列表用
    batch.stock_sector = stock_sector
    batch.stock_sector_status = stock_sector_status

    return batch
