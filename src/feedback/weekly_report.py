"""
周日周报
周度绩效复盘 + 观点挖掘汇总
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime, date, timedelta

from ..db import get_connection
from ..feedback.trade_logger import get_trade_logger
from ..decision.insight_miner import get_insight_miner
from ..push.pushplus import get_pushplus

logger = logging.getLogger(__name__)


class WeeklyReport:
    """周日周报"""

    def __init__(self):
        self._trade_logger = get_trade_logger()
        self._insight_miner = get_insight_miner()
        self._pushplus = get_pushplus()

    def generate(self) -> str:
        """
        生成周报

        Returns:
            周报文本
        """
        today = datetime.now()
        week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        logger.info("=== 周度报告 %s ~ %s ===", week_start, week_end)

        lines = []
        lines.append(f"📊 周度报告 {week_start} ~ {week_end}")
        lines.append("")

        # 1. 大盘走势回顾
        lines.append("📈 大盘走势:")
        lines.append(self._get_market_trend(week_start))
        lines.append("")

        # 2. 信号统计
        lines.append("📋 本周信号统计:")
        lines.append(self._get_weekly_signal_stats(week_start))
        lines.append("")

        # 3. 操作回顾
        lines.append("💼 操作回顾:")
        lines.append(self._get_weekly_operations(week_start))
        lines.append("")

        # 4. 持仓状态
        lines.append("📊 持仓状态:")
        lines.append(self._get_holding_status())
        lines.append("")

        # 5. 观点挖掘汇总
        lines.append("🔍 观点挖掘追踪:")
        lines.append(self._get_insight_summary())
        lines.append("")

        # 6. 下周关注
        lines.append("🔮 下周关注:")
        lines.append(self._get_next_week_focus())

        return "\n".join(lines)

    def run_and_push(self):
        """生成并推送周报"""
        report = self.generate()
        self._pushplus.send_weekly_report(report)
        logger.info("周报已推送")

    def _get_market_trend(self, week_start: str) -> str:
        """大盘走势回顾"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date, score, mode FROM market_score_history WHERE date >= ? ORDER BY date ASC",
                (week_start,),
            )
            rows = cursor.fetchall()
            if rows:
                lines = []
                mode_names = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}
                for row in rows:
                    lines.append(f"  {row['date']}: {row['score']:.1f}分 → {mode_names.get(row['mode'], row['mode'])}")
                avg_score = sum(r["score"] for r in rows) / len(rows)
                lines.append(f"  本周均值: {avg_score:.1f}分")
                return "\n".join(lines)
        except Exception:
            pass
        finally:
            conn.close()
        return "  数据暂无"

    def _get_weekly_signal_stats(self, week_start: str) -> str:
        """本周信号统计"""
        stats = self._trade_logger.get_signal_stats(7)
        if not stats:
            return "  本周无信号"

        lines = []
        for key, count in sorted(stats.items()):
            lines.append(f"  {key}: {count}条")
        return "\n".join(lines)

    def _get_weekly_operations(self, week_start: str) -> str:
        """本周操作回顾"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trade_logs WHERE date >= ? AND user_action = 'executed' ORDER BY date, time",
                (week_start,),
            )
            rows = cursor.fetchall()
            if rows:
                lines = []
                for row in rows:
                    lines.append(f"  {row['date']} {row['stock_name']} {row['signal_type']} @ {row.get('actual_price', 0) or row.get('trigger_price', 0):.2f}")
                return "\n".join(lines)
        except Exception:
            pass
        finally:
            conn.close()
        return "  本周无操作"

    def _get_holding_status(self) -> str:
        """持仓状态"""
        from ..config_models import load_config
        portfolio = load_config("portfolio.yaml")
        holdings = portfolio.get("holdings", [])
        total_asset = portfolio.get("total_asset", 0)

        if not holdings:
            return "  当前无持仓"

        holding_value = sum(h.get("shares", 0) * h.get("cost", 0) for h in holdings)
        position_ratio = holding_value / total_asset if total_asset > 0 else 0

        lines = []
        for h in holdings:
            lines.append(f"  {h.get('name', '')}({h.get('code', '')}): {h.get('shares', 0)}股, 成本{h.get('cost', 0):.2f}")
        lines.append(f"  总仓位: {position_ratio*100:.1f}%")

        return "\n".join(lines)

    def _get_insight_summary(self) -> str:
        """观点挖掘汇总"""
        summary = self._insight_miner.get_weekly_summary()

        lines = []

        if summary.get("confirming"):
            lines.append(f"🔥 兑现中（{len(summary['confirming'])}条）：")
            for j in summary["confirming"][:5]:
                lines.append(f"  • {j.get('judgment', '')}")

        if summary.get("tracking"):
            lines.append(f"⏳ 追踪中（{len(summary['tracking'])}条）：")
            for j in summary["tracking"][:5]:
                lines.append(f"  • {j.get('judgment', '')}")

        if summary.get("refuted"):
            lines.append(f"❌ 证伪（{len(summary['refuted'])}条）：")
            for j in summary["refuted"][:3]:
                lines.append(f"  • {j.get('judgment', '')}")

        confirm_rate = summary.get("confirm_rate", 0)
        lines.append(f"📈 累计观点兑现率: {confirm_rate*100:.0f}%")

        return "\n".join(lines) if lines else "  本周无观点追踪"

    def _get_next_week_focus(self) -> str:
        """下周关注"""
        lines = [
            "  关注大盘评分变化趋势",
            "  关注持仓股止损/止盈位",
            "  关注观点追踪池兑现进度",
        ]
        return "\n".join(lines)


# 单例
_instance: Optional[WeeklyReport] = None


def get_weekly_report() -> WeeklyReport:
    global _instance
    if _instance is None:
        _instance = WeeklyReport()
    return _instance
