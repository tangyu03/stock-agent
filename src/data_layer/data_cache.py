"""
数据缓存层
避免同一天内重复调用API，结果缓存到SQLite

P1-13: 改用 thread-local 连接池（get_conn 上下文管理器）
原：每次 get/set 新建+close 连接
新：复用 thread-local 连接，避免重复创建
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Any
from ..db import get_conn

logger = logging.getLogger(__name__)


class DataCache:
    """基于SQLite的数据缓存"""

    def __init__(self, default_ttl_hours: int = 8):
        self._default_ttl = timedelta(hours=default_ttl_hours)

    def get(self, cache_key: str) -> Optional[Any]:
        """从缓存获取数据"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT cache_value, expire_at FROM data_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()

                if row is None:
                    return None

                # 检查是否过期
                expire_at = datetime.fromisoformat(row["expire_at"]) if row["expire_at"] else None
                if expire_at and datetime.now() > expire_at:
                    cursor.execute("DELETE FROM data_cache WHERE cache_key = ?", (cache_key,))
                    conn.commit()
                    return None

                return json.loads(row["cache_value"])
        except Exception as e:
            logger.error("Cache get error for key '%s': %s", cache_key, e)
            return None

    def set(self, cache_key: str, value: Any, ttl_hours: Optional[int] = None):
        """写入缓存"""
        ttl = timedelta(hours=ttl_hours) if ttl_hours else self._default_ttl
        expire_at = (datetime.now() + ttl).isoformat()
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO data_cache (cache_key, cache_value, created_at, expire_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cache_key, json.dumps(value, ensure_ascii=False, default=str),
                     datetime.now().isoformat(), expire_at),
                )
                conn.commit()
        except Exception as e:
            logger.error("Cache set error for key '%s': %s", cache_key, e)

    def get_or_fetch(self, cache_key: str, fetch_func, ttl_hours: Optional[int] = None) -> Any:
        """缓存优先获取：先查缓存，未命中则调用fetch_func并缓存结果"""
        cached = self.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit: %s", cache_key)
            return cached

        logger.debug("Cache miss: %s, calling fetch_func", cache_key)
        result = fetch_func()

        if result is not None:
            self.set(cache_key, result, ttl_hours)

        return result

    def clear_expired(self):
        """清理过期缓存"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                cursor.execute("DELETE FROM data_cache WHERE expire_at < ?", (now,))
                deleted = cursor.rowcount
                conn.commit()
                logger.info("Cleared %d expired cache entries", deleted)
        except Exception as e:
            logger.error("Cache cleanup error: %s", e)

    def clear_all(self):
        """清空所有缓存"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM data_cache")
                conn.commit()
                logger.info("All cache cleared")
        except Exception as e:
            logger.error("Cache clear error: %s", e)


# 单例
_instance: Optional[DataCache] = None


def get_data_cache() -> DataCache:
    global _instance
    if _instance is None:
        _instance = DataCache()
    return _instance
