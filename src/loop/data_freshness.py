"""
DataFreshnessGuard — 通用数据新鲜度守卫（S系列阶段A，bankuai.md 补强二）

解决数据管道最阴险的故障模式："不报错、数据看起来正常、只是旧"（静默截断，
如概念指数默认 end_date 截断在 2025-02-28）。所有落盘数据强制携带 trade_date
字段；盘后统一断言"最后日期 == 最近交易日"，不满足则该数据源当日剔除计算并
由调用方推送告警。

与 F9 的"跳过数据未更新日/周末"逻辑合并：泛化 institutional_scorer 的
_find_recent_trading_day 为统一交易日工具，未来任何接口（含 THS 改版）的
静默截断都被此守卫罩住。

守卫两道网：
  1. 主网 check_source：断言源内 max(trade_date) == 最近交易日（盘后场景）
  2. 第二道网 check_fresh_enough：latest >= today-N（离线/回测/假期容差场景）
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 统一交易日工具（泛化自 institutional_scorer._find_recent_trading_day，F9）
# 注意：仅按周末跳过，不含法定假期。假期后首个交易日会判"过期"→ 触发告警
# 而非静默，属可接受的显性噪声（宁可报错不可静默）。
# ---------------------------------------------------------------------------
def find_recent_trading_day(target_date: str, max_lookback: int = 10,
                            skip_today: bool = False) -> str:
    """找 target_date 之前最近的交易日（周末/假期回退，返回 YYYY-MM-DD）。

    Args:
        target_date: 目标日期，支持 YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD
        max_lookback: 最多回退天数（防止无界循环）
        skip_today: True=跳过 target_date 本身（如"今天数据可能未更新"）
    """
    dt = _parse_date(target_date)
    if dt is None:
        logger.warning("[Freshness] 无法解析日期 %r，按原样返回", target_date)
        return str(target_date)
    start = 1 if skip_today else 0
    for i in range(start, max_lookback + start):
        d = dt - timedelta(days=i)
        if d.weekday() < 5:  # 周一~周五
            return d.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _parse_date(s) -> Optional[datetime]:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class DataFreshnessGuard:
    """统一新鲜度守卫。

    用法（盘后管道）：
        guard = DataFreshnessGuard()
        rows = ...  # 当日该源全部快照行，每行含 trade_date 字段
        if not guard.check_source("industry_summary", rows):
            # 当日剔除该源计算 + 推送告警（调用方负责）
    """

    def __init__(self, max_staleness_days: int = 5):
        self.max_staleness_days = max_staleness_days

    def check_source(self, source_name: str, rows: List[Dict],
                     trade_date_field: str = "trade_date",
                     skip_today: bool = False) -> bool:
        """主网：断言该源最后日期 == 最近交易日。

        Args:
            rows: 该源当日全部快照行（必须含 trade_date 字段）
            skip_today: True=源数据天然滞后一天（如融资余额）；False=盘后当日可得
        """
        latest = _latest_date(rows, trade_date_field)
        if latest is None:
            logger.warning("[Freshness] %s 缺 %s 字段，当日剔除",
                           source_name, trade_date_field)
            return False
        expected = find_recent_trading_day(
            datetime.now().strftime("%Y-%m-%d"), skip_today=skip_today)
        if latest == expected:
            logger.info("[Freshness] %s 新鲜: %s", source_name, latest)
            return True
        logger.warning("[Freshness] %s 过期: 最后=%s, 期望=%s → 当日剔除",
                       source_name, latest, expected)
        return False

    def check_fresh_enough(self, source_name: str, latest_date: str) -> bool:
        """第二道网（容差）：latest >= today - N。用于离线/回测/假期场景。"""
        latest_dt = _parse_date(latest_date)
        if latest_dt is None:
            logger.warning("[Freshness] %s 日期无法解析 %r → 判过期",
                           source_name, latest_date)
            return False
        cutoff = datetime.now() - timedelta(days=self.max_staleness_days)
        ok = latest_dt >= cutoff
        logger.info("[Freshness] %s 容差检查 latest=%s (cutoff=%s) → %s",
                    source_name, latest_date, cutoff.strftime("%Y-%m-%d"),
                    "OK" if ok else "过期")
        return ok


def _latest_date(rows: List[Dict], field: str) -> Optional[str]:
    if not rows:
        return None
    vals = [str(r.get(field, "")).strip() for r in rows if r.get(field)]
    if not vals:
        return None
    return max(vals)
