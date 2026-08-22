"""
板块快照数据结构

Step1 板块层面构建、Step2 个股层面读取的统一数据结构：
- BoardSector: 单个板块的状态（分类 + 涨跌幅 + 排名 + 指标快照）
- BoardSnapshot: 某一天的完整板块快照（sectors 状态 + 成分股反查索引）

成分股不内嵌在快照 JSON（90 行业约 5000+ 只，体量大），单独落 board_component 表；
BoardSnapshot 只承载板块状态，反查索引 stock_to_sectors 加载时由 component 聚合生成。
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class BoardSector:
    """单个板块在某一天的状态"""
    sector_key: str          # 主键：THS 代码，如 "881121"
    name: str                # 板块名，如 "半导体"
    source: str = "THS"      # "THS" / "eastmoney" / ...
    classification: str = "rotational"  # main_trend / rotational / retreating / unknown
    change_pct: float = 0.0  # 当日涨跌幅（构建时刻）
    rank: int = 0            # 当日排名
    total: int = 0           # 板块总数
    stock_count: int = 0     # 成分股数
    metrics: Dict = field(default_factory=dict)  # calc_sector_metrics 快照（可空）

    def to_row(self) -> dict:
        import json
        return {
            "sector_key": self.sector_key,
            "sector_name": self.name,
            "source": self.source,
            "classification": self.classification,
            "change_pct": self.change_pct,
            "rank": self.rank,
            "total": self.total,
            "stock_count": self.stock_count,
            "metrics_json": json.dumps(self.metrics, ensure_ascii=False),
        }


@dataclass
class BoardSnapshot:
    """某一天的完整板块快照"""
    snapshot_date: str                      # "2026-08-22"（构建日期，也是分片键）
    trade_date: str                         # 最近交易日（数据实际截止日，供新鲜度判断）
    created_at: str                         # ISO 时间戳
    source: str = "THS"
    sectors: Dict[str, BoardSector] = field(default_factory=dict)  # sector_key -> 状态
    stock_to_sectors: Dict[str, List[str]] = field(default_factory=dict)  # code -> [sector_key]
    stale: bool = False                     # 读取时若跨日则置 True
    component_count: int = 0

    def get_sector(self, sector_key: str) -> Optional[BoardSector]:
        return self.sectors.get(sector_key)
