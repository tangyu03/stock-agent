"""
P2-3: 统一 session 缓存工具
============================
替代各模块自己管理的 _xxx_cache / _xxx_cache_date 全局变量

特性：
- TTL 支持（秒级，默认1小时）
- 自动过期清理
- 线程安全（threading.Lock）
- 统一接口：get(key, fetch_func, ttl) / set(key, value) / clear()

使用方式：
    from src.utils.session_cache import get_session_cache
    cache = get_session_cache()
    result = cache.get_or_fetch("lhb_detail", fetch_lhb, ttl=3600)
"""
import threading
import time
from typing import Any, Callable, Optional, Dict
import logging

logger = logging.getLogger(__name__)


class SessionCache:
    """
    统一 session 缓存（内存级，进程结束后失效）
    替代各模块的 _xxx_cache 全局变量
    """

    def __init__(self):
        self._cache: Dict[str, dict] = {}  # {key: {"value": ..., "expire_at": float}}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存（过期返回None）"""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry["expire_at"] < time.time():
                del self._cache[key]
                return None
            return entry["value"]

    def set(self, key: str, value: Any, ttl: int = 3600):
        """写入缓存"""
        with self._lock:
            self._cache[key] = {
                "value": value,
                "expire_at": time.time() + ttl,
            }

    def get_or_fetch(self, key: str, fetch_func: Callable, ttl: int = 3600) -> Any:
        """
        缓存优先获取
        - 缓存命中：返回缓存
        - 缓存未命中：调用 fetch_func，缓存结果后返回
        """
        cached = self.get(key)
        if cached is not None:
            logger.debug("SessionCache hit: %s", key)
            return cached

        logger.debug("SessionCache miss: %s, calling fetch_func", key)
        result = fetch_func()
        if result is not None:
            self.set(key, result, ttl)
        return result

    def clear(self, prefix: str = ""):
        """清空缓存（可按前缀批量清除）"""
        with self._lock:
            if prefix:
                # 按前缀清除
                keys_to_del = [k for k in self._cache if k.startswith(prefix)]
                for k in keys_to_del:
                    del self._cache[k]
                logger.info("SessionCache cleared %d entries (prefix=%s)", len(keys_to_del), prefix)
            else:
                count = len(self._cache)
                self._cache.clear()
                logger.info("SessionCache cleared all %d entries", count)

    def stats(self) -> dict:
        """缓存统计"""
        with self._lock:
            return {
                "total_keys": len(self._cache),
                "keys": list(self._cache.keys())[:10],  # 只显示前10个
            }


# 单例
_instance: Optional[SessionCache] = None
_instance_lock = threading.Lock()


def get_session_cache() -> SessionCache:
    """获取 SessionCache 单例（线程安全）"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SessionCache()
    return _instance
