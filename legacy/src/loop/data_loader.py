"""
回测数据加载器

用现有的 AKShare 适配器拉历史K线，并整理成回测引擎需要的格式。
关键：补充 prev_close 字段（用于涨跌停判断），并把字段名统一。
"""
import logging
from typing import Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DataLoader:
    """回测数据加载器"""

    def __init__(self):
        from ..data_layer.akshare_adapter import get_akshare_adapter
        self._akshare = get_akshare_adapter()

    def load_kline(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict[str, List[Dict]]:
        """
        批量加载多只股票的历史K线

        Args:
            codes: 股票代码列表，如 ["600519", "000001"]
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD

        Returns:
            {code: [{"date": "YYYY-MM-DD", "open": ..., "close": ..., "high": ..., "low": ..., "volume": ..., "prev_close": ...}, ...]}
        """
        result = {}
        # 多加载 5 天，确保起始日期有前一日收盘价用于 prev_close
        actual_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y%m%d")
        actual_end = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d")

        for code in codes:
            logger.info("加载 %s K线数据 (%s ~ %s)", code, start_date, end_date)
            kline = self._load_single(code, actual_start, actual_end, start_date, end_date)
            if kline:
                result[code] = kline
            else:
                logger.warning("%s K线数据加载失败", code)

        return result

    def _load_single(
        self,
        code: str,
        fetch_start: str,
        fetch_end: str,
        want_start: str,
        want_end: str,
    ) -> List[Dict]:
        """加载单只股票K线，补充 prev_close 字段"""
        result_obj = self._akshare.get_stock_hist(
            code=code,
            start_date=fetch_start,
            end_date=fetch_end,
            adjust="qfq",  # 前复权
        )

        if not result_obj.success or not result_obj.data:
            logger.warning("%s: AKShare 返回失败 - %s", code, result_obj.error)
            return []

        rows = result_obj.data
        # 统一字段名（东财/新浪源字段名不同）
        normalized = []
        for r in rows:
            item = {
                "date": self._parse_date(r.get("日期") or r.get("date") or ""),
                "open": float(r.get("开盘", 0) or r.get("open", 0) or 0),
                "close": float(r.get("收盘", 0) or r.get("close", 0) or 0),
                "high": float(r.get("最高", 0) or r.get("high", 0) or 0),
                "low": float(r.get("最低", 0) or r.get("low", 0) or 0),
                "volume": float(r.get("成交量", 0) or r.get("volume", 0) or 0),
            }
            if item["date"] and item["close"] > 0:
                normalized.append(item)

        # 按日期排序
        normalized.sort(key=lambda x: x["date"])

        # 补充 prev_close
        for i in range(len(normalized)):
            if i == 0:
                normalized[i]["prev_close"] = normalized[i]["open"]  # 首日用开盘价兜底
            else:
                normalized[i]["prev_close"] = normalized[i - 1]["close"]

        # 过滤到目标日期范围
        filtered = [
            r for r in normalized
            if want_start <= r["date"] <= want_end
        ]

        logger.info("%s: 加载 %d 根K线（范围 %s ~ %s）",
                    code, len(filtered), want_start, want_end)
        return filtered

    @staticmethod
    def _parse_date(raw) -> str:
        """把各种日期格式统一成 YYYY-MM-DD"""
        if not raw:
            return ""
        s = str(raw).strip()
        # 处理 "2026-03-24" / "2026-03-24 00:00:00" / "20260324"
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""
