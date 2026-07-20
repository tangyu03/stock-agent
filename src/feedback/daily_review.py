"""
盘后复盘
每日收盘后自动生成复盘报告
"""
import logging
from typing import Dict, Any, Optional
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

        report = "\n".join(lines)
        return report

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
