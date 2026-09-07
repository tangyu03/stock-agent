"""
通用内存 TTL 缓存（线程安全）

进程内避免重复拉取/重复计算。按需读写：只在写入时占内存，不预加载。
过期的 key 在 get 时惰性删除，也可调用 clear_expired 主动清理。
"""
import threading
import time
from typing import Any, Dict, Optional, Tuple


class MemoryTTLCache:
    """线程安全的内存 TTL 缓存：key -> (value, expire_ts)"""

    def __init__(self, default_ttl_seconds: int = 300):
        self._default_ttl = default_ttl_seconds
        self._data: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """读取；已过期返回 None 并删除"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expire_ts = item
            if expire_ts is not None and time.time() > expire_ts:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        """写入；ttl_seconds=None 用默认 TTL，ttl_seconds=0 表示不过期"""
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        expire_ts = None if ttl == 0 else time.time() + ttl
        with self._lock:
            self._data[key] = (value, expire_ts)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def clear_expired(self) -> int:
        """清理已过期条目，返回清理数量"""
        now = time.time()
        removed = 0
        with self._lock:
            for k, (_, expire_ts) in list(self._data.items()):
                if expire_ts is not None and now > expire_ts:
                    del self._data[k]
                    removed += 1
        return removed

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
