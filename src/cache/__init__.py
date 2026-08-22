"""
板块映射缓存层（内存 TTL + SQLite 按天持久化）

三步协作：先查内存 → 未命中查 SQLite 按天快照 → 再未命中由编排层拉取并回填。
按需读写：只在读取时加载当天快照/反查 watchlist，不预加载整表。

- board_snapshot:  BoardSnapshot/BoardSector 数据结构
- ttl_cache:       进程内 TTL 缓存
- snapshot_store:  SQLite 持久化（board_snapshot / board_component 表）
- builders:        Step1 拉取纯逻辑（排名/成分股/K线分类）
- sector_map_service: 编排层（Step1 构建 / Step2 读取分类 / 惰性重建 / 降级链）
"""
from .board_snapshot import BoardSnapshot, BoardSector
from .snapshot_store import SnapshotStore
from .ttl_cache import MemoryTTLCache
from .sector_map_service import SectorMapService, get_sector_map_service

__all__ = [
    "BoardSnapshot",
    "BoardSector",
    "SnapshotStore",
    "MemoryTTLCache",
    "SectorMapService",
    "get_sector_map_service",
]
