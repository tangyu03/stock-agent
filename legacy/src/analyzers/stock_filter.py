"""
个股趋势过滤器 - 第三层
仅 ST/停牌 硬过滤，其余条件已下沉到 timing_engine / position_analyzer
"""
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from ..data_layer.akshare_adapter import get_akshare_adapter

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """前置检查结果（仅 ST/停牌）"""
    stock_code: str
    stock_name: str
    passed: bool
    failed_checks: List[str] = field(default_factory=list)  # 未通过的条件描述
    check_details: Dict[str, Any] = field(default_factory=dict)  # 各项检查详情
    is_holding: bool = False  # 是否持仓（持仓跳过过滤）


class StockFilter:
    """个股趋势过滤器 — 仅 ST/停牌 硬过滤。
    原 8 项中的其余 7 项已下沉：
    - 均线/价格/涨幅 → timing_engine 各策略内部判断
    - 流动性/事件/减持/财报 → position_analyzer 综合健康评分
    """

    def __init__(self):
        self._akshare = get_akshare_adapter()

    def prefetch_quotes(self, stock_codes: List[str]) -> None:
        """
        批量预取行情 -> 写入统一模块级缓存

        委托 stock_data.prefetch_quotes 执行（AKShare优先 -> 问财降级），
        各模块共享同一份缓存，消除重复实现（优化 #3）。
        _fetch_stock_data 通过 get_prefetched_quote() 读取。

        Args:
            stock_codes: 股票代码列表
        """
        if not stock_codes:
            return
        from ..data_layer.stock_data import prefetch_quotes as _prefetch
        _prefetch(stock_codes)

    def filter_stock(self, stock_code: str, stock_name: str = "", entry_type: str = "套利低吸") -> FilterResult:
        """
        对单只个股执行前置过滤

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            entry_type: 进场类型，影响涨幅阈值

        Returns:
            FilterResult
        """
        result = FilterResult(stock_code=stock_code, stock_name=stock_name, passed=True)

        # 获取个股数据
        stock_data = self._fetch_stock_data(stock_code)
        result.check_details = stock_data

        # 仅检查 ST/停牌
        checks = [
            self._check_st_and_suspended,
        ]
        for check_func in checks:
            try:
                passed, reason = check_func(stock_data, entry_type)
                if not passed:
                    result.passed = False
                    result.failed_checks.append(reason)
            except Exception as e:
                logger.warning("Filter check %s failed for %s: %s", check_func.__name__, stock_code, e)
                result.passed = False
                result.failed_checks.append(f"检查异常: {check_func.__name__}")

        if result.passed:
            logger.info("✅ %s(%s) 过滤通过", stock_name or stock_code, stock_code)
        else:
            logger.info("❌ %s(%s) 过滤未通过: %s", stock_name or stock_code, stock_code, result.failed_checks)

        return result

    def filter_batch(self, stocks: List[Dict], entry_type: str = "套利低吸") -> Dict[str, FilterResult]:
        """
        批量过滤

        Args:
            stocks: [{"code": "xxx", "name": "xxx"}, ...]
            entry_type: 进场类型

        Returns:
            {stock_code: FilterResult}
        """
        results = {}
        for stock in stocks:
            code = stock.get("code", "")
            name = stock.get("name", "")
            results[code] = self.filter_stock(code, name, entry_type)
        return results

    def _fetch_stock_data(self, stock_code: str) -> Dict[str, Any]:
        """获取个股数据（仅需 ST/停牌 判断）"""
        data = {"code": stock_code}

        # 优先从统一行情缓存获取 ST/停牌/名称
        from ..data_layer.stock_data import get_prefetched_quote
        cached = get_prefetched_quote(stock_code)
        if cached:
            data["is_st"] = cached.get("is_st", False)
            data["is_suspended"] = cached.get("is_suspended", False)
            data["stock_name"] = cached.get("name", "")
        else:
            # 缓存未命中 → 从 K 线数据推断名称/ST
            hist_result = self._akshare.get_stock_hist(stock_code)
            if hist_result.success and hist_result.data and len(hist_result.data) >= 1:
                last = hist_result.data[-1]
                name = str(last.get("名称", last.get("name", "")))
                data["stock_name"] = name
                data["is_st"] = "ST" in name or "*ST" in name
                data["is_suspended"] = False

        return data

    def _check_st_and_suspended(self, data: Dict, entry_type: str) -> tuple:
        """非ST/非*ST/非停牌"""
        is_st = data.get("is_st", False)
        is_suspended = data.get("is_suspended", False)
        name = data.get("stock_name", "")

        if is_st or "ST" in name or "*ST" in name:
            return False, "风险标的（ST/*ST）"
        if is_suspended:
            return False, "风险标的（停牌）"
        return True, ""

# 单例
_instance: Optional[StockFilter] = None


def get_stock_filter() -> StockFilter:
    global _instance
    if _instance is None:
        _instance = StockFilter()
    return _instance

