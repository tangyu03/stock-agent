"""
Orchestrator 调度引擎（v3 — pre_market 与 intraday 已合并）

节点：
  pre_market / intraday — 盘中统一检查（环境评估 + 板块 + 全量买卖信号，一次推送）
  post_market           — 盘后复盘
  weekly                — 周报
"""
from pathlib import Path
from typing import Optional, Tuple, Dict, List
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
# P2-13 审计（2026-08-22）：非交易日（周末）回退上一交易日的统一交易日工具
from ..loop.data_freshness import find_recent_trading_day

# logger = logging.getLogger(__name__)  # 原代码
logger = get_structured_logger(__name__)


# ================================================================
# P2 审计（2026-08-18）：代码版本戳 + 交易时段闸门
# ================================================================
_GIT_HEAD: Optional[str] = None


def _get_git_head() -> str:
    """缓存 git 短 commit，供日志/推送溯源代码版本"""
    global _GIT_HEAD
    if _GIT_HEAD is not None:
        return _GIT_HEAD
    import subprocess
    try:
        root = Path(__file__).resolve().parent.parent.parent
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=root, timeout=5,
        )
        _GIT_HEAD = out.stdout.strip() or "unknown"
    except Exception:
        _GIT_HEAD = "unknown"
    return _GIT_HEAD


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _resolve_run_context() -> Tuple[bool, str, str]:
    """P0-1 审计（2026-08-18 起，2026-08-22 修订）：盘中检查的运行上下文。

    返回 (是否执行, 原因, 参考交易日 ref_date)：
    - 交易日（周一~周五）07:00-16:00 窗口内 → 执行，ref_date=今天
    - 交易日窗口外（深夜/清晨）→ 默认跳过推送，防误触发；force=True 覆盖
    - 非交易日（周末）→ 照常执行，按上一交易日收盘数据复盘
      （用户期望：非交易日就查上一个交易日的情况，而非整段跳过）

    注：仅按周末判定非交易日（与 data_freshness 一致，不含法定假期）；
    节假日落在工作日时仍会执行，数据层自然回退到最近真实交易日。
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    ref_date = find_recent_trading_day(today)  # 今天非交易日时回退到上一交易日
    if now.weekday() >= 5:
        return True, f"非交易日（{_WEEKDAY_CN[now.weekday()]}），按上一交易日 {ref_date} 收盘数据执行", ref_date
    hhmm = now.hour * 60 + now.minute
    if 7 * 60 <= hhmm <= 16 * 60:
        return True, "", ref_date
    return False, f"非交易时段（本地 {now.strftime('%H:%M')}，窗口 07:00-16:00）", ref_date


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

    def run(self, phase: str, force: bool = False, **kwargs):
        """
        phase 取值:
          pre_market   — 盘前预案
          intraday     — 盘中统一信号检查
          post_market  — 盘后复盘
          weekly       — 周报
        force: 交易时段闸门（P0-1 审计）——True 时忽略非交易日/非时段限制强制推送
        """
        # P0 修复：为每次调度生成 trace_id，串联整条调用链
        trace_id = set_trace_id()
        logger.info("调度开始", extra={
            "phase": phase,
            "trace_id": trace_id,
        })
        # P2 审计：代码版本戳，推送/日志可溯源到 commit
        logger.info("运行代码版本: %s", _get_git_head(), extra={"git_head": _get_git_head()})
        try:
            if phase == "pre_market":
                self._do_pre_market_plan(force=force)
            elif phase == "intraday":
                self._do_intraday(force=force)
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

    def _do_pre_market_plan(self, force: bool = False):
        """盘前预案 → 已合并到盘中统一检查"""
        logger.info("====== 盘前预案 (委托盘中统一检查) ======")
        self._do_intraday(force=force)

    def _do_intraday(self, force: bool = False):
        """盘中统一检查：环境评估 + 全量自选池信号 + 合并推送

        P2-12 结构说明（217行，便于维护）：
        - 步骤1: 综合环境评估
        - 步骤2: 统一引擎（全量自选池）
        - 步骤3: 构建信号列表（entry/exit/observation）
        - 步骤4: P3实盘信号调度器
        - 步骤5: 合并推送

        P0-1 审计（2026-08-18 起，2026-08-22 修订）：运行闸门。交易日窗口外默认跳过
        （防深夜误触发，日志记录但不再消费推送配额、不落库）；非交易日（周末）不跳过，
        按上一交易日收盘数据照常执行复盘。force=True 可覆盖交易日窗口限制。
        """
        run_ok, reason, ref_date = _resolve_run_context()
        today_str = datetime.now().strftime("%Y-%m-%d")
        is_backfill = ref_date != today_str
        if not run_ok and not force:
            logger.warning(
                "跳过盘中统一检查推送：%s。如需强制运行：python -m src.main run --phase intraday --force",
                reason,
            )
            return
        if is_backfill:
            logger.warning("非交易日运行盘中检查：%s（推送仍会发出，数据为上一交易日）", reason)
        elif not run_ok:
            logger.warning("强制运行盘中检查：%s（推送仍会发出）", reason)
        logger.info("====== 盘中统一检查 ======")

        # ---- 1. 综合环境评估 ----
        env = {
            "market_mode": "defend", "market_score": 5.0, "position_limit": 0.5,
            "gem_sci_tech": None, "external_market": None, "style_spread": None,
        }
        try:
            from ..loop.market_mode_adaptive import get_market_mode_adaptive
            adaptive = get_market_mode_adaptive()
            env = adaptive.assess_daily(force_refresh=True, ref_date=ref_date)
        except Exception as e:
            logger.warning("自适应环境评估失败，回退到缓存模式: %s", e)
            ms = self._market_scorer.get_current_mode()
            env.update({
                "market_mode": ms.get("mode", "defend"),
                "market_score": ms.get("score", 5.0),
                "position_limit": ms.get("position_limit", 0.5),
            })

        # P2-13 审计：透出数据参考日，非交易日复盘时推送/日志可明确"上一交易日"口径
        env["ref_date"] = ref_date
        env["is_backfill"] = is_backfill

        market_mode = env.get("market_mode", "defend")

        # P0-3 审计：环境推导链全程可观测——模式/评分/仓位上限/降级原因一屏可见，
        # 任一隐藏降级或参数覆盖都会在这里暴露，不再"日志无解释"。
        _pl = env.get("position_limit", 0.5)
        _canonical_pl = {"attack": 0.8, "defend": 0.5, "retreat": 0.1}.get(market_mode, 0.5)
        logger.info(
            "环境推导: 模式=%s 评分=%.1f position_limit=%.2f 外盘降级=%s S3降级=%s 降级前模式=%s",
            market_mode,
            float(env.get("market_score", 0) or 0),
            _pl,
            env.get("shock_downgraded", False),
            env.get("s3_downgraded", False),
            env.get("mode_before_shock", market_mode),
        )
        if abs(_pl - _canonical_pl) > 1e-9:
            logger.warning(
                "position_limit=%.2f 与模式 %s 的规范映射 %.2f 不一致（P0-3 审计）："
                "环境推导链存在未记录覆盖，请核查 assess_daily/降级路径。",
                _pl, market_mode, _canonical_pl,
            )
        if env.get("mode_reason"):
            logger.info("模式判定依据: %s", env.get("mode_reason"))

        # ---- 2. 统一引擎（全量自选池） ----
        # 板块分类由 sector_ranker（涨跌幅百分位排名）统一完成，不再需要 scanner 预扫
        from .unified_engine import run_unified_analysis
        batch = run_unified_analysis(
            data_mode="realtime",
            market_mode=market_mode,
            sector_result=None,
            sector_map={},
            market_score=float(env.get("market_score", 0) or 0),
        )

        # ---- 3. 构建信号列表 ----
        sector_ranks = {}
        env["sectors"] = {}

        # 自选池（portfolio.yaml stocks）——用于识别无信号的"观察"股；买卖信号由统一引擎全量扫
        portfolio = load_config("portfolio.yaml")  # 直接用模块顶层已导入的 load_config
        all_holdings = portfolio.get("stocks") or []
        holdings_map = {s.get("code", ""): s for s in all_holdings if s.get("code")}
        signaled_codes = set()  # 有买入或卖出信号的股票

        entry_batch = []
        for sig in batch.entries:
            td = getattr(sig, "tech_data", {}) or {}
            signaled_codes.add(sig.stock_code)
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
                "market_score": td.get("market_score"),
                "kline_pattern": td.get("kline_pattern", []),
                "tech_signals": td.get("tech_signals", {}),
                "ma5": td.get("ma5"), "ma10": td.get("ma10"), "ma20": td.get("ma20"),
                "rsi": (td.get("tech_signals") or {}).get("rsi"),
                "adx": (td.get("tech_signals") or {}).get("adx"),
                "volume_ratio": td.get("volume_ratio", 0),
                "turnover_rate": td.get("turnover_rate", 0),
                "benchmark_price": getattr(sig, "benchmark_price", 0),
                "rrr_low": getattr(sig, "rrr_low", None),
                "rrr_high": getattr(sig, "rrr_high", None),
                "execution_plan": getattr(sig, "execution_plan", {}),
                # 【一】可证伪假说透传（模板渲染 + 落库）
                "hypothesis": getattr(sig, "hypothesis", {}) or {},
                "event_id": getattr(sig, "event_id", "") or "",
                "audience": getattr(sig, "audience", "empty") or "empty",
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
        # 【三】受众四选一之"持有"：持仓且无任何买卖信号 → 持有建议
        # 持仓来源 = 回执闭环聚合（executed 记录），买入事件只对空仓者成立
        holdings = []
        try:
            holdings = self._trade_logger.get_current_holdings() or []
        except Exception as e:
            logger.warning("持仓聚合不可用（按空仓调度）: %s", e)
        held_codes = {str(h.get("code") or "") for h in (holdings or [])}
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
            position_hint = ""
            if code in held_codes:
                position_hint = "持仓建议: 持有（无买卖信号触发）"
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
                "turnover_rate": td.get("turnover_rate", 0),
                "kline_pattern": td.get("kline_pattern", []),
                "institutional_holding": td.get("institutional_holding", {}),
                "market_score": td.get("market_score"),
                "rsi": (td.get("tech_signals") or {}).get("rsi"),
                "adx": (td.get("tech_signals") or {}).get("adx"),
                "note": (position_hint + " | " if position_hint else "")
                        + "买入: "
                        + batch.entry_diagnostics.get(
                            code, "未参与入场检查（风控过滤或数据缺失）"
                        )
                        + " | 卖出: "
                        + te._exit_diagnostics.get(code, "未参与卖出检查（数据缺失）"),
            })
        logger.info("信号汇总: 买入%d 卖出%d 观察%d (自选%d, 有信号%d)",
                    len(entry_batch), len(exit_batch), len(observation_batch),
                    len(all_holdings), len(signaled_codes))

        # ---- 4.5 P3: 实盘信号调度器（信号服务模式 + 受众分流 + 假说门 + 策略下线）----
        # 【三】受众：holdings 已在上文回执闭环聚合；
        # 买入事件只对空仓者成立，持仓者输出四选一。

        # 【六】策略下线判定（滚动50笔期望为负/胜率跌破盈亏平衡线 → 自动下线+告警）
        kill_report: Dict = {}
        offline_strategies: List = []
        try:
            from ..feedback.strategy_stats import evaluate_kill_switch
            kill_report = evaluate_kill_switch() or {}
            offline_strategies = [s for s, info in kill_report.items() if info.get("status") == "offline"]
        except Exception as e:
            logger.warning("策略下线评估不可用（按全策略调度）: %s", e)

        from ..decision.live_scheduler import schedule_live_signals, format_scheduled_summary
        total_asset = 1_000_000  # 兼容旧调用；纯信号模式不参与拦截
        scheduled = schedule_live_signals(
            entry_signals=entry_batch,
            exit_signals=exit_batch,
            total_asset=total_asset,
            market_mode=market_mode,
            holdings=holdings,
            offline_strategies=offline_strategies,
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

        # 【一】出厂拒绝留痕：假说不完整的信号写入 signal_rejections 表
        # （不进调度不推送，但可审计——可证伪性是信号出厂前的完整性检查）
        for rejection in getattr(batch, "rejected", []) or []:
            try:
                self._trade_logger.log_rejection(
                    stock_code=rejection.get("stock_code", ""),
                    stock_name=rejection.get("stock_name", ""),
                    entry_type=rejection.get("entry_type", ""),
                    reasons=rejection.get("reasons", []),
                    detail={
                        "benchmark_price": rejection.get("benchmark_price", 0),
                        "stop_loss": rejection.get("stop_loss", 0),
                        "target_range": rejection.get("target_range", []),
                        "hypothesis": rejection.get("hypothesis", {}),
                    },
                )
            except Exception as e:
                logger.warning("拒绝留痕失败: %s", e)

        # 【六】策略下线告警推送（一套不能被自己的业绩杀死的逻辑，不算被厘清）
        newly_offline = [
            (strategy, info)
            for strategy, info in (kill_report or {}).items()
            if info.get("newly_offline")
        ]
        if newly_offline:
            try:
                alert_lines = ["⛔ 策略自动下线（记录闭环判定）:"]
                for strategy, info in newly_offline:
                    alert_lines.append(f"  {strategy}: {info.get('reason', '')}")
                alert_lines.append("策略下线重校前不再产生新买入信号；已持仓出场不受影响。")
                self._pushplus.send(
                    "策略下线告警",
                    f"<pre>{chr(10).join(alert_lines)}</pre><p>代码版本 {_get_git_head()}</p>",
                    level="重要",
                )
            except Exception:
                pass

        # ---- 5. 合并推送（环境 + 调度后买卖信号 + 观察一条消息）----
        # 推送调度后的信号（而非原始全量信号）
        self._pushplus.send_intraday_report(env, scheduled_entry_batch, scheduled_exit_batch, observation_batch)

        # 额外推送调度摘要（让用户知道哪些信号被跳过及原因）
        if scheduled['skipped'] and any(scheduled['skipped'].values()):
            try:
                # P2 审计：摘要带代码版本，推送可溯源到 commit
                self._pushplus.send(
                    "信号调度摘要",
                    f"<pre>{schedule_summary}</pre><p>代码版本 {_get_git_head()}</p>",
                    level="常规",
                )
            except Exception:
                pass

        # ---- 5.5 P1-3 + 【六】记录闭环：推送后落库（user_action=pending）----
        # 每笔交易四行日志之第一行：假说原文（X/Y/Z/W + 整句）与配对价位，
        # 推送时同步落库（修复：原实现 stop_loss/target_price 从未写入，全部为 0）。
        # 回执后 update_action 联动回填 exit_price/pnl_pct/zw_triggered。
        # 去重：当日同股同类型 pending 已存在则跳过（盘中多次运行不重复落库）
        today = datetime.now().strftime("%Y-%m-%d")
        existing_pending = self._trade_logger.get_pending_signals(today)
        pending_keys = {(p["stock_code"], p["signal_type"], p["entry_type"] or p["exit_type"]) for p in existing_pending}
        logged = {"buy": 0, "sell": 0}
        for s in scheduled['buy']:
            key = (s.stock_code, "buy", s.entry_type or "")
            if key in pending_keys:
                continue
            hyp = s.hypothesis or {}
            w_range = [v for v in (hyp.get("w") or []) if v]
            if not w_range:
                plan_targets = (s.execution_plan or {}).get("target_range") or [0]
                w_range = [plan_targets[0] or 0]
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
                    # 【一】假说四要素 + 配对 Z/W 落库（记录闭环第一行）
                    "stop_loss": float(hyp.get("z") or 0) or s.execution_plan.get("stop_loss", 0),
                    "target_price": float(w_range[0] or 0),
                    "paired_z": float(hyp.get("z") or 0),
                    "paired_w_low": float(w_range[0] or 0),
                    "paired_w_high": float(w_range[-1] or 0),
                    "event_id": getattr(s, "event_id", "") or "",
                    "hypothesis": hyp,
                },
                shares=s.shares,
                note=(s.schedule_note + " | 假说: " + hyp.get("sentence", "")) if hyp.get("sentence") else s.schedule_note,
            )
            logged["buy"] += 1
        for s in scheduled['sell']:
            key = (s.stock_code, "sell", s.exit_type or "")
            if key in pending_keys:
                continue
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
                shares=0,  # 信号服务模式：不持有持仓数据，股数由用户自行决定
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
