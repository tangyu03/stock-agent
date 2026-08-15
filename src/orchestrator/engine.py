"""
Orchestrator 调度引擎（v3 — pre_market 与 intraday 已合并）

节点：
  pre_market / intraday — 盘中统一检查（环境评估 + 板块 + 全量买卖信号，一次推送）
  post_market           — 盘后复盘
  weekly                — 周报
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from ..config_models import load_config
from ..analyzers.market_scorer import get_market_scorer
from ..decision.aggregator import get_aggregator
from ..push.pushplus import get_pushplus
# templates 渲染已移至 pushplus.send_intraday_report 内部调用
from ..feedback.trade_logger import get_trade_logger
from ..feedback.daily_review import get_daily_review
from ..feedback.weekly_report import get_weekly_report
# P0 修复：引入结构化日志 + trace_id
from ..utils.structured_logger import get_structured_logger, set_trace_id, clear_trace_id

# logger = logging.getLogger(__name__)  # 原代码
logger = get_structured_logger(__name__)


class Orchestrator:
    """调度引擎 — 统一入口 run(phase)"""

    def __init__(self):
        self._market_scorer = get_market_scorer()
        self._aggregator = get_aggregator()
        self._pushplus = get_pushplus()
        self._trade_logger = get_trade_logger()
        self._daily_review = get_daily_review()
        self._weekly_report = get_weekly_report()

    # ================================================================
    # 统一入口
    # ================================================================

    def run(self, phase: str, **kwargs):
        """
        phase 取值:
          pre_market   — 盘前预案
          intraday     — 盘中统一信号检查
          post_market  — 盘后复盘
          weekly       — 周报
        """
        # P0 修复：为每次调度生成 trace_id，串联整条调用链
        trace_id = set_trace_id()
        logger.info("调度开始", extra={
            "phase": phase,
            "trace_id": trace_id,
        })
        try:
            if phase == "pre_market":
                self._do_pre_market_plan()
            elif phase == "intraday":
                self._do_intraday()
            elif phase == "post_market":
                self._do_post_market()
            elif phase == "weekly":
                self._do_weekly()
            else:
                logger.error("Unknown phase: %s", phase, extra={"phase": phase})
        except Exception as e:
            logger.error("调度异常: %s", e, extra={
                "phase": phase,
                "error_type": type(e).__name__,
            }, exc_info=True)
            raise
        finally:
            logger.info("调度结束", extra={"phase": phase, "trace_id": trace_id})
            clear_trace_id()

    # ================================================================
    # 节点 1：盘前预案 / 盘中统一检查（已合并）
    # ================================================================

    def _do_pre_market_plan(self):
        """盘前预案 → 已合并到盘中统一检查"""
        logger.info("====== 盘前预案 (委托盘中统一检查) ======")
        self._do_intraday()

    def _do_intraday(self):
        """盘中统一检查：环境评估 + 全量自选池信号 + 合并推送

        P2-12 结构说明（217行，便于维护）：
        - 步骤1: 综合环境评估
        - 步骤2: 统一引擎（全量自选池）
        - 步骤3: 构建信号列表（entry/exit/observation）
        - 步骤4: P3实盘信号调度器
        - 步骤5: 合并推送
        """
        logger.info("====== 盘中统一检查 ======")

        # ---- 1. 综合环境评估 ----
        env = {
            "market_mode": "defend", "market_score": 5.0, "position_limit": 0.5,
            "gem_sci_tech": None, "external_market": None, "style_spread": None,
        }
        try:
            from ..loop.market_mode_adaptive import get_market_mode_adaptive
            adaptive = get_market_mode_adaptive()
            env = adaptive.assess_daily(force_refresh=True)
        except Exception as e:
            logger.warning("自适应环境评估失败，回退到缓存模式: %s", e)
            ms = self._market_scorer.get_current_mode()
            env.update({
                "market_mode": ms.get("mode", "defend"),
                "market_score": ms.get("score", 5.0),
                "position_limit": ms.get("position_limit", 0.5),
            })

        market_mode = env.get("market_mode", "defend")

        # ---- 2. 统一引擎（全量自选池） ----
        # 板块分类由 sector_ranker（涨跌幅百分位排名）统一完成，不再需要 scanner 预扫
        from .unified_engine import run_unified_analysis
        batch = run_unified_analysis(
            data_mode="realtime",
            market_mode=market_mode,
            sector_result=None,
            sector_map={},
        )

        # ---- 3. 构建信号列表 ----
        sector_ranks = {}
        env["sectors"] = {}

        # 收集所有持仓股代码（用于识别无信号的"观察"股）
        from ..config_models import load_config
        portfolio = load_config("portfolio.yaml")
        all_holdings = portfolio.get("stocks") or []
        holdings_map = {s.get("code", ""): s for s in all_holdings if s.get("code")}
        signaled_codes = set()  # 有买入或卖出信号的股票

        entry_batch = []
        for sig in batch.entries:
            signaled_codes.add(sig.stock_code)
            td = getattr(sig, "tech_data", {}) or {}
            entry_batch.append({
                "stock_name": sig.stock_name or sig.stock_code,
                "stock_code": sig.stock_code,
                "entry_type": sig.entry_type,
                "trigger_price": sig.entry_trigger_price,
                "current_price": td.get("current_price", sig.entry_trigger_price),
                "change_pct": getattr(sig, "change_pct", None) or td.get("change_pct"),
                "stop_loss": sig.stop_loss,
                "target_range": sig.target_range,
                "position_level": sig.position_level,
                "sector_status": sig.sector_status,
                "sector_name": getattr(sig, "sector_name", "") or "",
                "sw_level2": getattr(sig, "sw_level2", "") or "",
                "note": sig.trigger_reason or "",
                "confidence": getattr(sig, "confidence", "中"),
                "market_mode": market_mode,
                "kline_pattern": td.get("kline_pattern", []),
                "tech_signals": td.get("tech_signals", {}),
                "ma5": td.get("ma5"), "ma10": td.get("ma10"), "ma20": td.get("ma20"),
                "rsi": (td.get("tech_signals") or {}).get("rsi"),
                "adx": (td.get("tech_signals") or {}).get("adx"),
                "volume_ratio": td.get("volume_ratio", 0),
                # 机构持仓打分（4 数据源投票 + 具体数值，透出到 push）
                "institutional_holding": td.get("institutional_holding", {}),
            })

        exit_batch = []
        for sig in batch.exits:
            if isinstance(sig, dict):
                exit_batch.append(sig)
                signaled_codes.add(sig.get("stock_code", ""))
            else:
                signaled_codes.add(sig.stock_code)
                td = getattr(sig, "tech_data", {}) or {}
                exit_batch.append({
                    "stock_name": sig.stock_name, "stock_code": sig.stock_code,
                    "exit_type": sig.exit_type, "trigger_price": getattr(sig, "trigger_price", 0),
                    "reason": getattr(sig, "reason", ""), "urgency": getattr(sig, "urgency", ""),
                    "market_mode": market_mode,
                    "sector_status": getattr(sig, "sector_status", ""),
                    "sector_name": getattr(sig, "sector_name", ""),
                    "sw_level2": getattr(sig, "sw_level2", "") or "",
                    "sector_rank": (sector_ranks.get(getattr(sig, "sector_name", ""), {}).get("rank") if sector_ranks else None),
                    "sector_rank_total": (sector_ranks.get(getattr(sig, "sector_name", ""), {}).get("total") if sector_ranks else None),
                    "current_price": td.get("current_price", getattr(sig, "trigger_price", 0)),
                    "change_pct": td.get("change_pct"),
                    "stop_loss_price": getattr(sig, "stop_loss_price", 0),
                    "kline_pattern": td.get("kline_pattern", []),
                    "tech_signals": td.get("tech_signals", {}),
                    "ma5": td.get("ma5"), "ma10": td.get("ma10"), "ma20": td.get("ma20"),
                    "rsi": td.get("rsi"), "adx": td.get("adx"),
                    "volume_ratio": td.get("volume_ratio", 0),
                    # 机构持仓打分（4 数据源投票 + 具体数值，透出到 push）
                    "institutional_holding": td.get("institutional_holding", {}),
                })

        # 构建观察列表：无买卖信号的持仓股
        # 从 timing_engine._tech_data_full 取技术面+机构资金数据（出场检查已算好，不重复调 API）
        from ..analyzers.timing_engine import get_timing_engine
        te = get_timing_engine()
        observation_batch = []
        for code, stock_info in holdings_map.items():
            if code in signaled_codes:
                continue  # 已有买卖信号，跳过
            name = stock_info.get("name", code)
            sector_name = batch.stock_sector.get(code, "")
            sector_status = batch.stock_sector_status.get(code, "rotational")
            # 从 tech_data_full 取技术面和机构资金数据（check_exit_signals 已缓存）
            td = te._tech_data_full.get(code, {})
            if not isinstance(td, dict):
                td = {}
            observation_batch.append({
                "stock_name": name,
                "stock_code": code,
                "current_price": td.get("current_price", 0),
                "change_pct": td.get("change_pct"),
                "sector_status": sector_status,
                "sector_name": sector_name,
                "market_mode": market_mode,
                "tech_signals": td.get("tech_signals", {}),
                "ma5": td.get("ma5"), "ma10": td.get("ma10"), "ma20": td.get("ma20"),
                "volume_ratio": td.get("volume_ratio", 0),
                "kline_pattern": td.get("kline_pattern", []),
                "institutional_holding": td.get("institutional_holding", {}),
                "note": "无买卖信号，持续观察",
            })

        logger.info("信号汇总: 买入%d 卖出%d 观察%d (持仓%d, 有信号%d)",
                    len(entry_batch), len(exit_batch), len(observation_batch),
                    len(all_holdings), len(signaled_codes))

        # ---- 4.5 P3: 实盘信号调度器（Step0 逻辑移植）----
        # 卖出优先 + 买入按期望排序 + 预算约束 + 主动放弃
        from ..decision.live_scheduler import schedule_live_signals, format_scheduled_summary
        # 从 trade_logger 读取真实持仓（P0-1：无已执行记录时按空仓运行，不再静默）
        holdings = []
        try:
            holdings = self._trade_logger.get_current_holdings() or []
        except Exception as e:
            logger.error("读取持仓失败，按空持仓处理: %s", e, exc_info=True)
            holdings = []
        if not holdings:
            logger.warning(
                "未读到任何已执行持仓（trade_logger 闭环未建立/add_plans 均未执行）——调度按空仓运行。"
                "买入合计已由 P0-2 总仓位闸门（position_limit=%.2f, 上限 %.0f 万）兜底，防止信号满载打到满仓。",
                env.get("position_limit", 0.5),
                env.get("position_limit", 0.5) * 1_000_000 / 10_000,
            )
        total_asset = 1_000_000  # 默认，后续可从 trade_logger 读
        try:
            account = self._trade_logger.get_account_summary() or {}
            total_asset = account.get('total_asset', 1_000_000)
        except Exception as e:
            logger.error("读取账户摘要失败，按默认总资产: %s", e)

        scheduled = schedule_live_signals(
            entry_signals=entry_batch,
            exit_signals=exit_batch,
            holdings=holdings,
            total_asset=total_asset,
            market_mode=market_mode,
            position_limit=env.get("position_limit", 0.5),
        )

        # 用调度后的信号替换原始信号推送
        scheduled_entry_batch = []
        for s in scheduled['buy']:
            # 找原始信号补充字段
            orig = next((e for e in entry_batch if e.get('stock_code') == s.stock_code), {})
            scheduled_entry_batch.append({
                **orig,
                'stock_code': s.stock_code,
                'stock_name': s.stock_name,
                'entry_type': s.entry_type,
                'trigger_price': s.trigger_price,
                'note': s.reason + f' | 调度: {s.schedule_note}',
                'confidence': s.confidence,
                'shares': s.shares,
                'expectancy': s.expectancy,
            })
        scheduled_exit_batch = []
        for s in scheduled['sell']:
            orig = next((e for e in exit_batch if e.get('stock_code') == s.stock_code), {})
            scheduled_exit_batch.append({
                **orig,
                'stock_code': s.stock_code,
                'stock_name': s.stock_name,
                'exit_type': s.exit_type,
                'trigger_price': s.trigger_price,
                'reason': s.reason,
                'urgency': s.urgency,
            })

        # 记录调度日志
        schedule_summary = format_scheduled_summary(scheduled)
        logger.info("信号调度完成:\n%s", schedule_summary)

        # ---- 5. 合并推送（环境 + 调度后买卖信号 + 观察一条消息）----
        # 推送调度后的信号（而非原始全量信号）
        self._pushplus.send_intraday_report(env, scheduled_entry_batch, scheduled_exit_batch, observation_batch)

        # 额外推送调度摘要（让用户知道哪些信号被跳过及原因）
        if scheduled['skipped'] and any(scheduled['skipped'].values()):
            try:
                self._pushplus.send("信号调度摘要", f"<pre>{schedule_summary}</pre>", level="常规")
            except Exception:
                pass

        # ---- 5.5 P1-3: 推送后落库（user_action=pending，等待回执脚本确认执行）----
        # 去重：当日同股同类型 pending 已存在则跳过（盘中多次运行不重复落库）
        today = datetime.now().strftime("%Y-%m-%d")
        existing_pending = self._trade_logger.get_pending_signals(today)
        pending_keys = {(p["stock_code"], p["signal_type"], p["entry_type"] or p["exit_type"]) for p in existing_pending}
        logged = {"buy": 0, "sell": 0}
        for s in scheduled['buy']:
            key = (s.stock_code, "buy", s.entry_type or "")
            if key in pending_keys:
                continue
            self._trade_logger.log_signal(
                signal_type="buy",
                stock_code=s.stock_code,
                stock_name=s.stock_name,
                signal_data={
                    "entry_type": s.entry_type,
                    "trigger_price": s.trigger_price,
                    "mode_at_signal": s.market_mode,
                    "market_score": env.get("market_score", 0),
                    "suggested_position": min(s.shares * s.trigger_price / max(total_asset, 1), 0.25),
                },
                shares=s.shares,
                note=s.schedule_note,
            )
            logged["buy"] += 1
        for s in scheduled['sell']:
            key = (s.stock_code, "sell", s.exit_type or "")
            if key in pending_keys:
                continue
            sell_hold = next((h for h in holdings if h.get("code") == s.stock_code), {})
            self._trade_logger.log_signal(
                signal_type="sell",
                stock_code=s.stock_code,
                stock_name=s.stock_name,
                signal_data={
                    "exit_type": s.exit_type,
                    "trigger_price": s.trigger_price,
                    "mode_at_signal": s.market_mode,
                    "market_score": env.get("market_score", 0),
                },
                shares=sell_hold.get("shares", 0),
                note=s.reason or s.schedule_note,
            )
            logged["sell"] += 1
        if logged["buy"] or logged["sell"]:
            logger.info("信号已落库待回执: 买%d 卖%d", logged["buy"], logged["sell"])

        stats = scheduled['stats']
        logger.info("盘中检查完成 (模式=%s 调度后: 进场%d 出场%d, 跳过 买%d 卖%d)",
                    market_mode, stats['buy_executed'], stats['sell_executed'],
                    stats['entry_in'] - stats['buy_executed'],
                    stats['sell_in'] - stats['sell_executed'])


    def _do_post_market(self):
        """盘后复盘"""
        logger.info("====== 盘后复盘 ======")
        try:
            review = self._daily_review.generate()
            self._pushplus.send("每日复盘", review, level="常规")
        except Exception as e:
            logger.error("每日复盘失败: %s", e, exc_info=True)

    def process_user_article(self, text: str, source: str = ""):
        """处理用户文章/观点（委托给 insight_miner）"""
        from ..decision.insight_miner import get_insight_miner
        miner = get_insight_miner()
        return miner.process_article(text, source)

    def _do_weekly(self):
        """周报"""
        logger.info("====== 周报 ======")
        try:
            report = self._weekly_report.generate()
            self._pushplus.send("周度报告", report, level="常规")
        except Exception as e:
            logger.error("周报失败: %s", e, exc_info=True)


# 单例
_instance: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _instance
    if _instance is None:
        _instance = Orchestrator()
    return _instance
