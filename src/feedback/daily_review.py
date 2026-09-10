"""
盘后复盘
每日收盘后自动生成复盘报告
"""
import logging
from typing import Optional
from datetime import datetime, date

from ..db import get_connection
from ..decision.aggregator import get_aggregator
from ..feedback.trade_logger import get_trade_logger
from ..push.pushplus import get_pushplus

logger = logging.getLogger(__name__)


class DailyReview:
    """盘后复盘"""

    def __init__(self):
        self._aggregator = get_aggregator()
        self._logger = get_trade_logger()
        self._pushplus = get_pushplus()

    def generate(self) -> str:
        """
        生成盘后复盘报告

        Returns:
            复盘报告文本
        """
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info("=== 盘后复盘 %s ===", today)

        lines = []
        lines.append(f"📈 盘后复盘 {today}")
        lines.append("")

        # 0.【P1-3】广播表置顶：状态机说了算，不维护持仓；只对空仓系统
        # 输出条件句。它取代“27 条止损未触发”的幽灵持仓体检。
        lines.append("📡 在飞事件:")
        try:
            from ..feedback.event_tracker import (
                collect_in_flight_events,
                render_in_flight_events,
            )
            in_flight = collect_in_flight_events(as_of=date.today(), lookback_days=7)
            lines.append(render_in_flight_events(in_flight))
        except Exception as e:
            lines.append(f"  在飞事件生成失败: {str(e)[:60]}")
        lines.append("")

        # 0.1 【P1-3】昨日事件追踪表（决策记录：收盘版顶部出现表格——
        # 没有记忆的系统无法校准任何参数，作废条件的判定都依赖它先存在）
        lines.append("📅 昨日事件追踪（近2日，含闭合记录）:")
        try:
            from ..feedback.event_tracker import (
                build_event_tracking_table, render_event_tracking_table,
            )
            rows = build_event_tracking_table(as_of=date.today(), lookback_days=2)
            lines.append(render_event_tracking_table(rows))
        except Exception as e:
            lines.append(f"  追踪表生成失败: {str(e)[:60]}")
        lines.append("")

        # 0.5 【P0-1】外推误差回填：收盘比对"外推全天量 vs 实际全天量"，
        # 误差进记录表（作废条件的数据基础设施）
        try:
            from ..analyzers.volume_projection import finalize_day_projection
            from ..data_layer.stock_data import get_stock_data
            finalized = finalize_day_projection(self._collect_actual_ratios())
            if finalized:
                lines.append(f"  外推误差回填: {finalized} 条（收盘 vs 盘中外推，已入记录表）")
        except Exception as e:
            logger.debug("外推误差回填失败: %s", str(e)[:60])

        # 1. 今日大盘
        lines.append("📊 大盘表现:")
        market_summary = self._get_market_summary()
        lines.append(market_summary)
        lines.append("")

        # 2. 今日信号统计
        lines.append("📋 今日信号:")
        signal_summary = self._get_signal_summary()
        lines.append(signal_summary)
        lines.append("")

        # 3. 持仓变动
        lines.append("💼 持仓变动:")
        holding_summary = self._get_holding_summary()
        lines.append(holding_summary)
        lines.append("")

        # 4. 做T统计
        lines.append("🔄 做T统计:")
        t0_summary = self._get_t0_summary()
        lines.append(t0_summary)
        lines.append("")

        # 5. 明日关注
        lines.append("🔮 明日关注:")
        tomorrow_focus = self._get_tomorrow_focus()
        lines.append(tomorrow_focus)

        # 5.1 【P3-2】到期检查点回填；失败留待下一次，不造数。
        try:
            from ..feedback.observation_tracker import fill_observation_checkpoints
            fill_result = fill_observation_checkpoints(as_of=date.today())
            if fill_result.get("missing"):
                lines.append("")
                lines.append(
                    f"拦截票检查点回填: 新增{fill_result.get('filled', 0)}条，"
                    f"{fill_result['missing']}条价格未取到"
                )
        except Exception as e:
            logger.debug("拦截票检查点回填失败: %s", str(e)[:80])

        # 5.2 【P3-4】昨日复核 + 失效条款；缺失数据显式暴露。
        lines.append("")
        try:
            from ..data_layer.stock_data import batch_get_realtime_quotes
            from ..feedback.daily_review_parts import (
                build_yesterday_observation_review,
                format_invalidation_clause,
                previous_weekday,
            )
            review_env = {}
            try:
                from ..analyzers.market_env import get_market_environment
                review_env["market_env"] = get_market_environment(force_refresh=False)
            except Exception as e:
                logger.debug("失效条款市场环境获取失败: %s", str(e)[:80])
            try:
                from ..analyzers.gem_sci_tech_scorer import get_gem_sci_tech_analysis
                review_env["gem_sci_tech"] = get_gem_sci_tech_analysis(force_refresh=False)
            except Exception as e:
                logger.debug("失效条款双创数据获取失败: %s", str(e)[:80])

            yesterday = previous_weekday(date.today())
            quote_rows = batch_get_realtime_quotes(self._get_observation_t0_codes(yesterday))
            prices = {
                code: float(quote.get("current_price") or 0)
                for code, quote in quote_rows.items() if quote
            }
            market_env = review_env.get("market_env") or {}
            lines.append(build_yesterday_observation_review(
                yesterday,
                prices,
                market_env.get("csi300_change_pct"),
            ))
            lines.append(format_invalidation_clause(review_env))
        except Exception as e:
            lines.append(f"昨日复核/失效条款生成失败: {str(e)[:80]}")

        # 6.【P3-3】参数附录：每个参数的出处与验证状态
        # （RRR 1.5 隐含 40% 胜率假设七天未验证——参数没有验证状态
        #   就是拍脑袋的数字；附录机制永不作废）
        try:
            from ..feedback.param_appendix import build_param_appendix
            lines.append("")
            lines.append(build_param_appendix())
        except Exception as e:
            logger.debug("参数附录生成失败: %s", str(e)[:60])

        report = "\n".join(lines)
        return report

    def _get_observation_t0_codes(self, track_date: str):
        from ..feedback.observation_tracker import get_observation_intercepts
        return [row["stock_code"] for row in get_observation_intercepts(track_date=track_date)]

    def _collect_actual_ratios(self) -> dict:
        """【P0-1】收盘后取各股实际全天量/60日均量（用于外推误差回填）。"""
        actual: dict = {}
        try:
            codes = [
                str(s.get("code", ""))
                for s in (self._logger.get_current_holdings() or [])
            ]
        except Exception:
            codes = []
        if not codes:
            return actual
        try:
            from ..analyzers.timing_engine import get_timing_engine
            engine = get_timing_engine()
            engine.reset_caches()
            for code in codes[:30]:  # 预算：最多回填 30 只
                tech = engine._fetch_tech_data(code, "defend")
                if not isinstance(tech, dict):
                    continue
                today_volume = tech.get("today_volume")
                volume_ma60 = tech.get("volume_ma60")
                if today_volume and volume_ma60 and volume_ma60 > 0:
                    actual[code] = float(today_volume) / float(volume_ma60)
        except Exception as e:
            logger.debug("实际量口径采集失败: %s", str(e)[:60])
        return actual

    def run_and_push(self):
        """生成并推送复盘"""
        report = self.generate()
        self._pushplus.send_daily_review(report)
        logger.info("盘后复盘已推送")

    def _get_market_summary(self) -> str:
        """大盘概况"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = date.today().isoformat()
            cursor.execute(
                "SELECT score, mode, details FROM market_score_history WHERE date = ?",
                (today,),
            )
            row = cursor.fetchone()
            if row:
                mode_names = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}
                return f"  评分: {row['score']:.1f}/10 → {mode_names.get(row['mode'], row['mode'])}模式"
        except Exception:
            pass
        finally:
            conn.close()
        return "  数据暂无"

    def _get_signal_summary(self) -> str:
        """信号统计"""
        logs = self._logger.get_today_logs()
        if not logs:
            return "  今日无信号"

        buy_count = sum(1 for l in logs if l.get("signal_type") == "buy")
        sell_count = sum(1 for l in logs if l.get("signal_type") == "sell")
        t0_count = sum(1 for l in logs if l.get("signal_type") in ("t0_buy", "t0_sell"))
        executed = sum(1 for l in logs if l.get("user_action") == "executed")
        ignored = sum(1 for l in logs if l.get("user_action") == "ignored")

        lines = [
            f"  买入信号: {buy_count}条",
            f"  卖出信号: {sell_count}条",
            f"  做T信号: {t0_count}条",
            f"  执行/忽略: {executed}/{ignored}",
        ]

        # 信号详情
        for log in logs:
            action_emoji = {"executed": "✅", "ignored": "⏭️", "pending": "⏳"}.get(log.get("user_action", ""), "❓")
            lines.append(f"  {action_emoji} {log.get('stock_name', '')} {log.get('signal_type', '')} @ {log.get('trigger_price', 0):.2f}")

        return "\n".join(lines)

    def _get_holding_summary(self) -> str:
        """持仓概况"""
        from ..config_models import load_config
        portfolio = load_config("portfolio.yaml")
        # P0 修复：portfolio.yaml 用 stocks 而非 holdings（原代码永远返回空列表）
        holdings = portfolio.get("stocks") or portfolio.get("holdings") or []

        if not holdings:
            return "  当前无持仓"

        lines = []
        for h in holdings:
            if not isinstance(h, dict):
                continue
            code = h.get("code", "")
            name = h.get("name", code)
            shares = h.get("shares", 0)
            cost = h.get("cost", 0)
            # P0 修复：未提供 shares/cost 时仅显示跟踪信息
            if shares > 0 or cost > 0:
                lines.append(f"  {name}({code}): {shares}股, 成本{cost:.2f}")
            else:
                lines.append(f"  {name}({code}): 跟踪中（未录入持仓数量）")

        return "\n".join(lines)

    def _get_t0_summary(self) -> str:
        """做T统计"""
        logs = self._logger.get_today_logs()
        t0_logs = [l for l in logs if l.get("signal_type") in ("t0_buy", "t0_sell")]

        if not t0_logs:
            return "  今日无做T操作"

        # 按股票分组
        by_stock = {}
        for l in t0_logs:
            code = l.get("stock_code", "")
            if code not in by_stock:
                by_stock[code] = {"name": l.get("stock_name", ""), "rounds": 0}
            by_stock[code]["rounds"] += 1

        lines = []
        for code, info in by_stock.items():
            lines.append(f"  {info['name']}({code}): {info['rounds']}轮T")

        return "\n".join(lines)

    def _get_tomorrow_focus(self) -> str:
        """明日关注"""
        # 基于当前持仓和自选，列出明日需关注的事项
        lines = [
            "  关注持仓股是否触及止损/止盈",
            "  关注自选A类择时信号",
            "  关注做T候选标的竞价情况",
        ]
        return "\n".join(lines)


# 单例
_instance: Optional[DailyReview] = None


def get_daily_review() -> DailyReview:
    global _instance
    if _instance is None:
        _instance = DailyReview()
    return _instance
