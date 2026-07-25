"""
P0-6: akshare 安全调用包装器
============================
问题：14+处直接 import akshare as ak; ak.xxx() 无超时保护，可能hang死
方案：提供 call_ak_with_timeout(func, *args, timeout=30, **kwargs) 包装器

使用方式：
    from src.data_layer.akshare_safe import call_ak_with_timeout
    import akshare as ak
    df = call_ak_with_timeout(ak.stock_zh_index_daily, symbol="sh000001", timeout=30)

注意：超时后线程不会真正停止（Python无法kill线程），但主线程不再等待。
     akshare调用最终会因socket超时返回。
"""
import logging
import concurrent.futures
from typing import Any, Callable

logger = logging.getLogger(__name__)


def call_ak_with_timeout(func: Callable, *args, timeout: float = 30.0, **kwargs) -> Any:
    """
    带超时的 akshare 调用

    Args:
        func: akshare 函数（如 ak.stock_zh_index_daily）
        *args, **kwargs: 函数参数
        timeout: 超时秒数（默认30）

    Returns:
        函数返回值；超时或异常返回 None

    Note:
        超时后线程不会真正停止，但主线程不再等待。
        akshare 调用最终会因 socket 超时返回。
    """
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                logger.warning("akshare 调用超时(%ds): %s", timeout, func.__name__)
                return None
    except Exception as e:
        logger.warning("akshare 调用异常 %s: %s", func.__name__, str(e)[:80])
        return None


def safe_ak_func(func_name: str, timeout: float = 30.0):
    """
    创建带超时的 akshare 函数包装器

    Args:
        func_name: akshare 函数名（如 "stock_zh_index_daily"）
        timeout: 超时秒数

    Returns:
        包装后的函数

    Example:
        stock_zh_index_daily = safe_ak_func("stock_zh_index_daily")
        df = stock_zh_index_daily(symbol="sh000001")
    """
    import akshare as ak
    func = getattr(ak, func_name, None)
    if func is None:
        logger.error("akshare 无此函数: %s", func_name)
        return lambda *a, **kw: None

    def wrapper(*args, **kwargs):
        return call_ak_with_timeout(func, *args, timeout=timeout, **kwargs)

    wrapper.__name__ = f"safe_{func_name}"
    return wrapper
