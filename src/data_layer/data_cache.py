"""
数据缓存层
避免同一天内重复调用API，结果缓存到SQLite
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Any
from ..db import get_connection

logger = logging.getLogger(__name__)


class DataCache:
    """基于SQLite的数据缓存"""

    def __init__(self, default_ttl_hours: int = 8):
        self._default_ttl = timedelta(hours=default_ttl_hours)

    def get(self, cache_key: str) -> Optional[Any]:
        """
        从缓存获取数据

        Args:
            cache_key: 缓存键，格式如 "akshare:zt_pool:20260612"

        Returns:
            缓存的数据（已反序列化），过期或不存在返回None
        """
        conn = get_connection()
        try:
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
                # 过期，删除
                cursor.execute("DELETE FROM data_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
                return None

            return json.loads(row["cache_value"])

        except Exception as e:
            logger.error("Cache get error for key '%s': %s", cache_key, e)
            return None
        finally:
            conn.close()

    def set(
        self,
        cache_key: str,
        value: Any,
        ttl_hours: Optional[int] = None,
    ):
        """
        写入缓存

        Args:
            cache_key: 缓存键
            value: 要缓存的数据（会自动JSON序列化）
            ttl_hours: 缓存有效期（小时），不传使用默认值
        """
        ttl = timedelta(hours=ttl_hours) if ttl_hours else self._default_ttl
        expire_at = (datetime.now() + ttl).isoformat()

        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO data_cache (cache_key, cache_value, created_at, expire_at)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, json.dumps(value, ensure_ascii=False, default=str), datetime.now().isoformat(), expire_at),
            )
            conn.commit()
        except Exception as e:
            logger.error("Cache set error for key '%s': %s", cache_key, e)
        finally:
            conn.close()

    def get_or_fetch(self, cache_key: str, fetch_func, ttl_hours: Optional[int] = None) -> Any:
        """
        缓存优先获取：先查缓存，未命中则调用fetch_func并缓存结果

        Args:
            cache_key: 缓存键
            fetch_func: 数据获取函数（缓存未命中时调用）
            ttl_hours: 缓存有效期

        Returns:
            缓存或新获取的数据
        """
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
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute("DELETE FROM data_cache WHERE expire_at < ?", (now,))
            deleted = cursor.rowcount
            conn.commit()
            logger.info("Cleared %d expired cache entries", deleted)
        except Exception as e:
            logger.error("Cache cleanup error: %s", e)
        finally:
            conn.close()

    def clear_all(self):
        """清空所有缓存"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM data_cache")
            conn.commit()
            logger.info("All cache cleared")
        except Exception as e:
            logger.error("Cache clear error: %s", e)
        finally:
            conn.close()


# 单例
_instance: Optional[DataCache] = None


def get_data_cache() -> DataCache:
    global _instance
    if _instance is None:
        _instance = DataCache()
    return _instance
