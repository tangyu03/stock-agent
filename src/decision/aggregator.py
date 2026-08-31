"""
四层决策聚合器
串联五大任务 → 信号汇总 → 风控守卫 → 推送

【P0 修复清单】
1. 引入 HoldingHealth / WatchlistAnalysisResult（来自新建的 holding_health.py）
2. 引入 PositionAnalyzer（来自新建的 position_analyzer.py），初始化 self._position_analyzer
3. 修复 holdings 变量未定义（统一用 stocks 局部变量）
4. 修复 _build_sector_classification_map 中 stocks → holdings 参数名
5. 修复 _convert_v3_signals 对 _get_stocks() 返回 dict 的兼容（原代码用 hasattr 检查
   但 dict 分支未覆盖 name_map 构建）
6. position_builder.create_add_plan / append_add_plan 已在 position_builder.py 补全
7. 任务⑤中 holding_analyses 引用前确保已定义（防御性初始化为 []）
"""
from __future__ import annotations
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime

from ..config_models import load_config
from ..analyzers.market_scorer import get_market_scorer
# P1-17: sector_scanner 已废弃，统一用 sector_ranker
from ..analyzers.timing_engine import EntrySignal, ExitSignal
from .holding_health import HoldingHealth, WatchlistAnalysisResult
from .position_analyzer import get_position_analyzer
from .position_builder import PositionBuildSignal, get_position_builder

logger = logging.getLogger(__name__)


@dataclass
class SignalSummary:
    """信号汇总"""
    date: str
    market_mode: str
    market_score: float
    position_limit: float

    # 持仓信号
    holding_analyses: List[HoldingHealth] = field(default_factory=list)
    exit_signals: List[ExitSignal] = field(default_factory=list)

    # 自选信号
    watchlist_analyses: List[WatchlistAnalysisResult] = field(default_factory=list)
    entry_signals: List[EntrySignal] = field(default_factory=list)

    # 加仓信号（position_builder 产出）
    position_build_signals: List[PositionBuildSignal] = field(default_factory=list)

    # 做T信号（占位，由T0引擎填充）
    t0_signals: List[Dict] = field(default_factory=list)

    # 交叉诊断（P1-17 前 sector_scanner.CrossDiagnosisResult；scanner 废弃后该字段不再有生产者，
    #         保留字段仅为兼容序列化/调试输出）
    cross_diagnosis: List[Any] = field(default_factory=list)

    # 板块扫描（P1-17 前 sector_scanner.SectorScanResult；现恒为 None，unified_engine 直接管板块分类）
    sector_result: Optional[Any] = None

    # 盘前计划摘要
    pre_market_summary: str = ""

    # 风格轮动（task①附加）
    _style_spread: Optional[Dict] = None

    # 模式判定原因（真实数据驱动）
    mode_reason: str = ""

    # 双创技术位（task①附加，gem_sci_tech_scorer 结果）
    _gem_sci_tech: Optional[Dict] = None

    # 外围市场扰动（task①附加，external_market 结果）
    _external_market: Optional[Dict] = None


class Aggregator:
    """四层决策聚合器（P0 修复版）"""

    def __init__(self):
        self._portfolio_config = load_config("portfolio.yaml")
        self._market_scorer = get_market_scorer()
        # P0 修复：初始化 _position_analyzer
        self._position_analyzer = get_position_analyzer()

    def _get_stocks(self) -> List[Dict]:
        """获取 portfolio.yaml 中的 stocks 列表（持仓 + 自选统一来源）"""
        return (load_config("portfolio.yaml").get("stocks") or [])

    def run_daily_analysis(self, watchlist_codes=None) -> SignalSummary:
        """
        v3 调优版每日分析

        与原版的差异：
        1. 多模式自适应（上证指数）替代 market_scorer
        2. 板块扫描用申万行业数据（不依赖问财）
        3. 持仓/自选分析用 timing_engine 出场信号，不再依赖问财配额
        4. v3 自适应策略产生信号
        """
        import time as _time
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info("====== v3 每日分析开始 %s ======", today)

        summary = SignalSummary(
            date=today, market_mode="defend", market_score=5.0, position_limit=0.5,
        )
        index_kline = []

        # 任务①：多模式自适应
        logger.info("--- 任务①：多模式自适应 ---")
        try:
            # P0-6: 改用 safe_ak_func 带超时保护
            from ..data_layer.akshare_safe import safe_ak_func
            from ..loop.market_mode_adaptive import get_market_mode_adaptive
            stock_zh_index_daily = safe_ak_func("stock_zh_index_daily", timeout=30)
            df = stock_zh_index_daily(symbol="sh000001")
            if df is not None and len(df) >= 20:
                for _, r in df.tail(30).iterrows():
                    date_str = str(r["date"])
                    if hasattr(r["date"], "strftime"):
                        date_str = r["date"].strftime("%Y-%m-%d")
                    index_kline.append({
                        "date": date_str, "open": float(r["open"]), "close": float(r["close"]),
                        "high": float(r["high"]), "low": float(r["low"]), "volume": float(r["volume"]),
                    })
                if index_kline:
                    mode_adaptive = get_market_mode_adaptive()
                    today_str = index_kline[-1]["date"]
                    # P2-6: 一次评分同时取模式与真实连续分数（score_dimensions 含 raw_score 0-10，
                    # 与主路径 assess_daily 同源），不再硬编码 attack8/defend5/retreat2 近似，
                    # 消除"评分卡死 5.0"的表象（defend 只表示档位，分数反映真实 5 维推导）
                    dim_result = mode_adaptive.score_dimensions(today_str, index_kline)
                    if not dim_result:
                        mode = "defend"
                        summary.market_mode = mode
                        summary.mode_reason = ""
                        summary.position_limit = 0.5
                        summary.market_score = 5.0
                    else:
                        mode = dim_result.get("mode", "defend")
                        summary.market_mode = mode
                        summary.mode_reason = dim_result.get("mode_reason", "")
                        summary.position_limit = {"attack": 0.8, "defend": 0.5, "retreat": 0.1}.get(mode, 0.5)
                        summary.market_score = dim_result.get("raw_score", 5.0)
                    logger.info("自适应模式: %s（上证指数 %s 收盘 %s，评分 %.1f）",
                                mode, today_str, index_kline[-1]["close"], summary.market_score)
                    self._save_market_score(today_str, summary.market_score, mode)

                    # 大小盘风格轮动
                    style = mode_adaptive.get_style_spread()
                    summary._style_spread = style
                    logger.info("风格轮动: %s%s（spread=%.2f%%, trend=%s, 回归风险=%s）",
                                style["style"], style.get("style_strength", ""),
                                style["spread"], style["trend"],
                                style.get("mean_reversion_risk", "低"))

                    # 双创技术位分析
                    try:
                        gem_sci_tech = mode_adaptive.get_gem_sci_tech_analysis()
                        summary._gem_sci_tech = gem_sci_tech
                        logger.info("双创技术位: %s (risk=%s)",
                                    gem_sci_tech.get("trend_judgment", ""),
                                    gem_sci_tech.get("risk_flag", ""))
                    except Exception as e:
                        logger.warning("双创技术位分析失败: %s", e)

                    # 外围市场扰动
                    try:
                        from ..analyzers.external_market import get_external_market_assessment
                        ext = get_external_market_assessment()
                        summary._external_market = ext
                        disturbance = ext.get("disturbance", {})
                        logger.info("外围市场: %s", disturbance.get("summary", "无数据"))
                    except Exception as e:
                        logger.warning("外围市场分析失败: %s", e)
        except Exception as e:
            logger.warning("自适应模式判定失败：%s", e)

        # 任务②：持仓分析（P0 修复：_position_analyzer 已初始化）
        logger.info("--- 任务②：持仓分析 ---")
        # P0 修复：问财配额检查保留（桩对象永远返回可用）
        iwencai_available = not (
            hasattr(self._position_analyzer._skill, '_api_cooldown_until')
            and self._position_analyzer._skill._api_cooldown_until > 0
            and self._position_analyzer._skill._api_cooldown_until > _time.time()
        )
        if not iwencai_available:
            logger.warning("问财 API 配额耗尽，持仓分析降级为 timing_engine 出场信号")

        # P0 修复：stocks 是持仓+自选的统一来源，holdings 即 stocks
        stocks = self._portfolio_config.get("stocks") or []
        holdings = stocks  # P0 修复：原代码 holdings 未定义

        # 自动检测并填充持仓的板块（缓存30天）
        self._auto_fill_sectors(stocks, holdings)

        # P0 修复：sector_result 在原代码可能为 None 时 _build_sector_classification_map 不调用
        sector_classifications: Dict[str, str] = {}
        if summary.sector_result:
            sector_classifications = self._build_sector_classification_map(
                summary.sector_result, holdings, holdings  # P0 修复：原代码 watchlist 未定义
            )
# P1-17: cross_diagnose 已移除（统一用 sector_ranker）

        holding_codes = [s.get("code") for s in stocks if s and isinstance(s, dict) and s.get("code")]
        if holding_codes:
            self._position_analyzer.prefetch_quotes(holding_codes)

        # P0 修复：调用 PositionAnalyzer.analyze_all_holdings（已实现）
        holding_analyses = self._position_analyzer.analyze_all_holdings(
            holdings, summary.market_mode, sector_classifications
        )
        summary.holding_analyses = holding_analyses
        for h in holding_analyses:
            summary.exit_signals.extend(h.exit_signals)

        # 任务③：v3 自适应策略（额外信号源，不替代自选分析）
        logger.info("--- 任务③：v3 自适应策略 ---")
        v3_signals = []
        v3_entry_codes: set = set()
        v3_exit_codes: set = set()
        try:
            v3_signals = self._run_v3_strategy(watchlist_codes, index_kline, {})
            v3_entry, v3_exit = self._convert_v3_signals(v3_signals, summary.market_mode)
            v3_entry_codes = {s.stock_code for s in v3_entry}
            v3_exit_codes = {s.stock_code for s in v3_exit}
            logger.info("v3 信号: 买入 %d 条, 卖出 %d 条", len(v3_entry), len(v3_exit))
        except Exception as e:
            logger.warning("v3 策略失败：%s", e)
            v3_entry, v3_exit = [], []

        # 任务④：自选分析（统一路径：全部自选跑完整体检+入场信号，不因v3成功而跳过）
        logger.info("--- 任务④：自选分析 ---")
        watchlist_codes_to_fetch = [
            item.get("code", "") if isinstance(item, dict) else getattr(item, "code", "")
            for item in (holdings or [])
            if (isinstance(item, dict) and item.get("code"))
              or (hasattr(item, "code") and item.code)
        ]
        if watchlist_codes_to_fetch:
            from ..analyzers.stock_filter import get_stock_filter
            get_stock_filter().prefetch_quotes(watchlist_codes_to_fetch)

        watchlist_analyses: List[WatchlistAnalysisResult] = []  # deprecated，保留为空列表

        # 将 v3 买入信号合并到自选分析（补充入场信号，不覆盖已有分析）
        for wa in watchlist_analyses:
            code = wa.stock_code
            if code in v3_entry_codes:
                wa.should_push = True
                v3_count = sum(1 for s in v3_signals if getattr(s, "code", "") == code)
                if wa.push_reason:
                    wa.push_reason = f"[v3] {wa.push_reason}"
                else:
                    wa.push_reason = f"v3买入信号({v3_count}条)"
                existing_types = {s.entry_type for s in wa.entry_signals}
                for vs in v3_entry:
                    if vs.stock_code == code and vs.entry_type not in existing_types:
                        wa.entry_signals.append(vs)
                        existing_types.add(vs.entry_type)
            if code in v3_exit_codes:
                wa.should_push = True
                if "v3卖出" not in (wa.push_reason or ""):
                    wa.push_reason = "v3卖出信号 | " + (wa.push_reason or "")

        summary.watchlist_analyses = watchlist_analyses

        # 收集所有入场信号：自选分析 + v3（去重合并）
        for wa in watchlist_analyses:
            summary.entry_signals.extend(wa.entry_signals)
        summary.entry_signals.extend(v3_entry)
        summary.exit_signals.extend(v3_exit)

        # 全局去重合并：同一只股票跨源信号合并原因
        merged_entry: Dict[str, EntrySignal] = {}
        for sig in summary.entry_signals:
            code = sig.stock_code
            if code in merged_entry:
                existing = merged_entry[code]
                existing.trigger_reason = "；".join(dict.fromkeys([
                    existing.trigger_reason, sig.trigger_reason
                ]))
                seen_patterns = {p.get("pattern") for p in existing.kline_patterns if isinstance(p, dict)}
                for p in (sig.kline_patterns or []):
                    if isinstance(p, dict) and p.get("pattern") not in seen_patterns:
                        existing.kline_patterns.append(p)
                        seen_patterns.add(p.get("pattern"))
                logger.debug("合并买入原因 %s: %s", code, sig.trigger_reason)
            else:
                merged_entry[code] = sig
        summary.entry_signals = list(merged_entry.values())

        # 卖出信号：合并 reason
        merged_exit: Dict[str, Any] = {}
        for sig in summary.exit_signals:
            code = sig.stock_code
            if code in merged_exit:
                existing = merged_exit[code]
                import re as _re
                def _split_reason(r: str):
                    m = _re.search(r'\n\s*(?:推导:\s*|\[模式=)', r)
                    if m:
                        return r[:m.start()].strip(), r[m.start():].strip()
                    return r.strip(), ""
                core_old, _ = _split_reason(existing.reason)
                core_new, _ = _split_reason(sig.reason)
                if core_new and core_new != core_old:
                    existing.reason = "；".join(dict.fromkeys([existing.reason, core_new]))
                logger.debug("合并卖出原因 %s: %s", code, core_new)
            else:
                merged_exit[code] = sig
        summary.exit_signals = list(merged_exit.values())

        # 任务④.5：为新的进场信号自动创建啄米加仓计划（P0 修复：方法已存在）
        # （total_asset 由 position_builder 内部读取配置获取，本函数无需自取）
        pb = get_position_builder()
        new_plans_created = 0
        for sig in summary.entry_signals:
            try:
                td = getattr(sig, 'tech_data', {}) or {}
                if td and td.get("current_price"):
                    plan = pb.create_add_plan(
                        stock_code=sig.stock_code,
                        stock_name=sig.stock_name,
                        entry_price=sig.entry_trigger_price,
                        tech_data=td,
                    )
                    if plan:
                        pb.append_add_plan(plan)
                        new_plans_created += 1
            except Exception as e:
                logger.debug("创建加仓计划失败 %s: %s", sig.stock_code, e)
        if new_plans_created:
            logger.info("创建了 %d 个啄米加仓计划", new_plans_created)

        # 任务⑤：加仓信号（position_builder — 对持仓+自选检查套利加仓 + 计划触发）
        logger.info("--- 任务⑤：加仓信号 ---")
        try:
            build_signals: List[PositionBuildSignal] = []

            # 5a：加仓计划触发检查由 timing_engine 统一负责（原占位缓存构建已清理为死代码）

            # 5b：对持仓标的检查加仓信号（套利加仓）
            # 加仓信号生成由 timing_engine._check_arbitrage_entry 负责，
            # unified_engine 全量扫已覆盖，这里不再有额外收集逻辑
            # （原 no-op 循环体已清理：只赋值不消费的死代码）

            summary.position_build_signals = build_signals
            logger.info("加仓信号: %d 条（仅持仓股）", len(build_signals))
        except Exception as e:
            logger.warning("加仓信号检查失败: %s", e)

        logger.info("全局跨源合并: %d 条买入, %d 条卖出",
                    len(summary.entry_signals), len(summary.exit_signals))

        # 生成摘要
        summary.pre_market_summary = self._generate_pre_market_summary(summary)

        # 注入个股板块/三级行业字段到所有信号（概念模块已移除）
        code_lookup: Dict[str, Dict] = {}
        for h in (stocks or []):
            if isinstance(h, dict) and h.get("code"):
                code_lookup[h["code"]] = h

        for sig in summary.entry_signals:
            h = code_lookup.get(sig.stock_code, {})
            sig.sw_level2 = h.get("sw_level2", "")
            sig.sw_level3 = h.get("sw_level3", "")

        for sig in summary.exit_signals:
            h = code_lookup.get(sig.stock_code, {})
            sig.sw_level2 = h.get("sw_level2", "")
            sig.sw_level3 = h.get("sw_level3", "")

        logger.info("====== v3 每日分析完成 ======")
        return summary

    def _run_v3_strategy(
        self,
        watchlist_codes: Optional[List[str]],
        index_kline: List[Dict],
        sector_state_map: Dict[str, str] = None,
    ) -> List:
        """运行 v3 自适应策略，返回原始信号列表"""
        from ..loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals
        from ..loop.data_loader import DataLoader
        from datetime import datetime, timedelta

        if watchlist_codes is None:
            # P0 修复：_get_stocks 返回 List[Dict]，提取 code
            watchlist_codes = [s.get("code", "") for s in self._get_stocks() if s.get("code")]

        if not watchlist_codes:
            logger.info("v3 策略：自选为空，跳过")
            return []

        logger.info("v3 策略：待分析标的 %d 只，复用板块分类 %d 个",
                    len(watchlist_codes), len(sector_state_map or {}))

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        loader = DataLoader()
        kline_data = loader.load_kline(watchlist_codes, start_date, end_date)

        if not kline_data:
            logger.warning("v3 策略：K 线数据全部加载失败，跳过")
            return []

        engine = StockAgentTunedV3Signals(
            adaptive_mode=True,
            index_kline=index_kline,
            sector_state_map=sector_state_map or {},
            params={"backtest_mode": False},
        )

        signals = engine.generate_signals(kline_data)
        logger.info("v3 策略产生 %d 条原始信号", len(signals))
        return signals

    def _convert_v3_signals(
        self,
        v3_signals: List,
        market_mode: str,
    ) -> tuple:
        """将 v3 策略的原始 Signal 对象转换为 EntrySignal / ExitSignal"""
        from ..analyzers.timing_engine import EntrySignal, ExitSignal, get_timing_engine

        # 构建代码→名称映射（持仓 + 自选）
        # P0 修复：_get_stocks 返回 List[Dict]，正确提取 code/name
        name_map: Dict[str, str] = {}
        for s in (self._portfolio_config.get("stocks") or []):
            code = s.get("code", "")
            if code:
                name_map[code] = s.get("name", "")
        for item in self._get_stocks():
            # P0 修复：item 是 dict，原代码用 hasattr 检查但 dict 分支缺失
            if isinstance(item, dict):
                code = item.get("code", "")
                if code:
                    name_map[code] = item.get("name", "")
            elif hasattr(item, "code"):
                code = item.code
                if code:
                    name_map[code] = getattr(item, "name", "")

        # 只保留今日信号
        today_str = datetime.now().strftime("%Y-%m-%d")
        v3_signals = [s for s in v3_signals if getattr(s, "date", "") == today_str]
        logger.info("v3 信号过滤到今日 (%s): %d 条", today_str, len(v3_signals))

        # 为买入信号预取技术面数据
        te = get_timing_engine()
        _tech_cache: Dict[str, Dict] = {}
        for sig in v3_signals:
            code = sig.code if hasattr(sig, "code") else ""
            if getattr(sig, "action", "") == "buy" and code and code not in _tech_cache:
                try:
                    _tech_cache[code] = te._fetch_tech_data(code)
                except Exception:
                    _tech_cache[code] = {}

        entry_signals = []
        exit_signals = []

        for sig in v3_signals:
            code = sig.code if hasattr(sig, "code") else ""
            name = name_map.get(code, code)
            reason = (sig.reason or "") if hasattr(sig, "reason") else ""

            if sig.action == "buy":
                entry_type = "套利低吸"
                if "恐慌抄底" in reason:
                    entry_type = "恐慌抄底"
                elif "确认追强" in reason:
                    entry_type = "确认追强"
                elif "套利低吸" in reason:
                    entry_type = "套利低吸"
                elif "加仓" in reason:
                    entry_type = "套利低吸"

                stop_loss = round(sig.price * 0.95, 2)  # 兜底

                kline_pats = getattr(sig, "kline_patterns", None) or []
                td = _tech_cache.get(code, {})

                entry_signals.append(EntrySignal(
                    stock_code=code,
                    stock_name=name,
                    entry_type=entry_type,
                    entry_trigger_price=round(sig.price, 2),
                    stop_loss=stop_loss,
                    target_type="冲高止盈" if entry_type == "套利低吸" else "持有观察",
                    target_range=[],
                    position_level="normal",
                    applicable_modes=[market_mode],
                    trigger_reason=reason,
                    confidence="中",
                    kline_patterns=kline_pats or td.get("kline_pattern", []),
                    tech_data=td,
                ))

            elif sig.action == "sell":
                exit_type = "冲高止盈"
                urgency = "重要"
                if "止损" in reason:
                    exit_type = "破位止损"
                    urgency = "紧急"
                elif "认错" in reason:
                    exit_type = "主动认错"

                exit_signals.append(ExitSignal(
                    stock_code=code,
                    stock_name=name,
                    exit_type=exit_type,
                    trigger_price=round(sig.price, 2),
                    stop_loss_price=round(sig.price * 0.95, 2),
                    reason=reason,
                    urgency=urgency,
                    mode_constrained=False,
                ))

        logger.info("v3 信号转换: 买入 %d 条, 卖出 %d 条", len(entry_signals), len(exit_signals))
        return (entry_signals, exit_signals)

    def _save_market_score(self, date_str: str, score: float, mode: str):
        """保存大盘评分到数据库"""
        import json
        from ..db import get_connection
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO market_score_history (date, score, mode, details) VALUES (?, ?, ?, ?)",
                (date_str, score, mode, json.dumps({"source": "adaptive_v3"}, ensure_ascii=False)),
            )
            conn.commit()
        except Exception as e:
            logger.warning("保存大盘评分失败: %s", e)
        finally:
            conn.close()

    def _auto_fill_sectors(self, holdings: List[Dict], watchlist: List):
        """自动检测并填充持仓的板块 + SW 三级行业（概念模块已移除）"""
        from ..data_layer.sw_industry import fetch_stock_sector, fetch_stock_sw_industry_full

        for h in (holdings or []):
            if not isinstance(h, dict):
                continue
            code = h.get("code", "")
            if not code:
                continue
            if not h.get("sector"):
                sector = fetch_stock_sector(code)
                if sector:
                    h["sector"] = sector
                    logger.debug("自动检测 %s(%s) 板块: %s", h.get("name", ""), code, sector)
            if not h.get("sw_level2") or not h.get("sw_level3"):
                try:
                    full = fetch_stock_sw_industry_full(code)
                    if full.get("level2"):
                        h["sw_level2"] = full["level2"]
                    if full.get("level3"):
                        h["sw_level3"] = full["level3"]
                except Exception:
                    pass

    def _build_sector_classification_map(
        self,
        sector_result: Any,
        holdings: List[Dict],
        watchlist: List,
    ) -> Dict[str, str]:
        """构建 股票代码→板块状态 映射（只按板块判定，概念模块已移除）"""
        sector_class = {}
        for sr in sector_result.sectors:
            sector_class[sr.name] = sr.classification.value

        code_to_sector_status: Dict[str, str] = {}

        # P0 修复：原代码引用未定义的 stocks，改为参数 holdings
        for h in (holdings or []):
            if not h or not isinstance(h, dict):
                continue
            code = h.get("code", "")
            sector_name = h.get("sector", "")
            if sector_name in sector_class:
                code_to_sector_status[code] = sector_class[sector_name]

        for item in (watchlist or []):
            code = item.code if hasattr(item, 'code') else item.get("code", "")
            if hasattr(item, 'sector') and hasattr(item, 'code'):
                if item.sector and item.sector in sector_class:
                    code_to_sector_status[item.code] = sector_class[item.sector]
            elif isinstance(item, dict):
                sector = item.get("sector", "")
                if sector and sector in sector_class and code:
                    code_to_sector_status[code] = sector_class[sector]

        return code_to_sector_status

    def _generate_pre_market_summary(self, summary: SignalSummary) -> str:
        """生成盘前计划摘要文本"""
        lines = []

        mode_names = {"attack": "🟢 进攻", "defend": "🟡 防守", "retreat": "🔴 撤退"}
        mode_name = mode_names.get(summary.market_mode, summary.market_mode)
        lines.append(f"📊 大盘判断: {mode_name}模式")
        if summary.mode_reason:
            lines.append(f"   判定依据: {summary.mode_reason}")
        style = getattr(summary, '_style_spread', None) or {}
        if style:
            emoji = "🔵" if style.get("style") == "小盘" else "🔴" if style.get("style") == "大盘" else "⚪"
            lines.append(f"{emoji} 风格偏向: {style.get('style','')}{style.get('style_strength','')}"
                        f" (spread={style.get('spread',0):+.1f}%, {style.get('trend','')})")
        lines.append("")

        if summary.sector_result:
            lines.append("📈 板块概况:")
            if summary.sector_result.main_trend_sectors:
                lines.append(f"  主线: {', '.join(summary.sector_result.main_trend_sectors)}")
            if summary.sector_result.rotational_sectors:
                lines.append(f"  支线: {', '.join(summary.sector_result.rotational_sectors)}")
            if summary.sector_result.retreating_sectors:
                lines.append(f"  退潮: {', '.join(summary.sector_result.retreating_sectors)}")
            lines.append("")

        if summary.holding_analyses:
            lines.append("💼 持仓体检:")
            for h in summary.holding_analyses:
                rating_emoji = {"健康": "✅", "观察": "👀", "警告": "⚠️", "危险": "🔴"}
                emoji = rating_emoji.get(h.rating, "")
                pnl_str = f"+{h.pnl_ratio*100:.1f}%" if h.pnl_ratio >= 0 else f"{h.pnl_ratio*100:.1f}%"
                lines.append(f"  {emoji} {h.stock_name}({h.stock_code}) {h.rating} 浮盈{pnl_str} {h.mode_adjustment}")
            lines.append("")

        if summary.exit_signals:
            lines.append("🚨 卖出信号:")
            for sig in summary.exit_signals:
                lines.append(f"  [{sig.urgency}] {sig.stock_name} {sig.exit_type} - {sig.reason}")
            lines.append("")

        if summary.entry_signals:
            lines.append("📥 买入信号:")
            for sig in summary.entry_signals:
                lines.append(f"  {sig.stock_name} {sig.entry_type} 触发价{sig.entry_trigger_price:.2f} 止损{sig.stop_loss:.2f}")
            lines.append("")

        if summary.watchlist_analyses:
            lines.append("📋 自选股分析:")
            for wa in summary.watchlist_analyses:
                status = "通过" if (wa.filter_result and wa.filter_result.passed) else "风险"
                if wa.filter_result and not wa.filter_result.passed:
                    risks = "; ".join(wa.filter_result.failed_checks[:2])
                    lines.append(f"  [{status}] {wa.stock_name}({wa.stock_code}) - {risks}")
                elif wa.filter_result and wa.filter_result.passed:
                    lines.append(f"  [{status}] {wa.stock_name}({wa.stock_code})")
                else:
                    lines.append(f"  [?] {wa.stock_name}({wa.stock_code}) - 数据不足")
                if wa.push_reason:
                    lines.append(f"       → {wa.push_reason}")
            lines.append("")

        return "\n".join(lines)


# 单例
_instance: Optional[Aggregator] = None


def get_aggregator() -> Aggregator:
    global _instance
    if _instance is None:
        _instance = Aggregator()
    return _instance
