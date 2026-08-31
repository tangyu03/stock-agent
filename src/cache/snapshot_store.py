"""
快照 SQLite 持久化层

board_snapshot（板块状态）/ board_component（成分股）两张表的读写。
- 按天逻辑分片：snapshot_date 列 + 复合主键 + 索引
- 按需读写：load_stock_sectors 只反查传入的 codes，不整表载入
- latest() 取最近一天快照并返回滞后天数（供 stale 判断）
- prune() 清理早于保留窗口的快照，绝不删当天

注意：只在本线程做 DB 读写（thread-local 连接池，P1-13，worker 线程不碰 get_connection）。
"""
import json
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .board_snapshot import BoardSnapshot, BoardSector

logger = logging.getLogger(__name__)


def _normalize_date(d: str) -> str:
    """归一化日期为 YYYY-MM-DD"""
    return str(d)[:10]


class SnapshotStore:
    """板块快照 SQLite 读写"""

    def __init__(self, db_get_connection=None):
        # 允许注入连接工厂便于测试；默认用 src.db.get_connection
        if db_get_connection is None:
            from ..db import get_connection
            db_get_connection = get_connection
        self._get_conn = db_get_connection

    # ---------------- 存在性 / 读取 ----------------

    def has(self, snapshot_date: str) -> bool:
        d = _normalize_date(snapshot_date)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM board_snapshot WHERE snapshot_date = ? LIMIT 1",
            (d,),
        ).fetchone()
        return row is not None

    def load(self, snapshot_date: str) -> Optional[BoardSnapshot]:
        """加载某天的板块状态快照（不含成分股反查索引，用 load_stock_sectors 按需查）"""
        d = _normalize_date(snapshot_date)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM board_snapshot WHERE snapshot_date = ?", (d,),
        ).fetchall()
        if not rows:
            return None

        sectors: Dict[str, BoardSector] = {}
        component_count = 0
        for r in rows:
            metrics = {}
            if r["metrics_json"]:
                try:
                    metrics = json.loads(r["metrics_json"])
                except Exception:
                    metrics = {}
            sectors[r["sector_key"]] = BoardSector(
                sector_key=r["sector_key"],
                name=r["sector_name"],
                source=r["source"] or "THS",
                classification=r["classification"],
                change_pct=r["change_pct"] or 0.0,
                rank=r["rank"] or 0,
                total=r["total"] or 0,
                stock_count=r["stock_count"] or 0,
                metrics=metrics,
            )
            component_count += r["stock_count"] or 0

        return BoardSnapshot(
            snapshot_date=d,
            trade_date=d,
            created_at=rows[0]["created_at"] or "",
            source="THS",
            sectors=sectors,
            component_count=component_count,
        )

    def latest(self, max_stale_days: int = 5) -> Tuple[Optional[BoardSnapshot], int]:
        """
        取最近一天快照。
        Returns:
            (快照, 滞后天数)：滞后 0 = 当天新鲜，>0 = stale；None = 无可用
        """
        conn = self._get_conn()
        today = date.today().isoformat()
        rows = conn.execute(
            "SELECT DISTINCT snapshot_date FROM board_snapshot "
            "WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT ?",
            (today, max_stale_days + 1),
        ).fetchall()
        if not rows:
            return None, 0

        best_date = None
        best_lag = None
        for r in rows:
            d = _normalize_date(r["snapshot_date"])
            lag = (date.fromisoformat(today) - date.fromisoformat(d)).days
            if lag < 0:
                continue
            if best_lag is None or lag < best_lag:
                best_lag = lag
                best_date = d

        if best_date is None:
            return None, 0
        snap = self.load(best_date)
        if snap is None:
            return None, 0
        return snap, best_lag

    # ---------------- 成分股反查 ----------------

    def load_stock_sectors(
        self, snapshot_date: str, codes: List[str]
    ) -> Dict[str, List[str]]:
        """反查：code -> [sector_key, ...]（只查传入 codes，按需 SQL）"""
        d = _normalize_date(snapshot_date)
        if not codes:
            return {}
        result: Dict[str, List[str]] = {c: [] for c in codes}
        conn = self._get_conn()
        # 分批 IN 查询，避免 SQL 参数过多
        batch_size = 200
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT stock_code, sector_key FROM board_component "
                f"WHERE snapshot_date = ? AND stock_code IN ({placeholders})",
                (d, *batch),
            ).fetchall()
            for r in rows:
                result.setdefault(r["stock_code"], []).append(r["sector_key"])
        return result

    # ---------------- 写入 ----------------

    def save(self, snap: BoardSnapshot) -> None:
        """写入某天快照：board_snapshot 全量替换当日行 + board_component 当日全量重写"""
        d = _normalize_date(snap.snapshot_date)
        conn = self._get_conn()
        cursor = conn.cursor()

        # 板块状态：当日全量替换（PRIMARY KEY (snapshot_date, sector_key) 去重）
        cursor.execute("DELETE FROM board_snapshot WHERE snapshot_date = ?", (d,))
        for sector in snap.sectors.values():
            row = sector.to_row()
            cursor.execute(
                "INSERT INTO board_snapshot "
                "(snapshot_date, sector_key, sector_name, source, classification, "
                " change_pct, rank, total, stock_count, metrics_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d, row["sector_key"], row["sector_name"], row["source"],
                 row["classification"], row["change_pct"], row["rank"],
                 row["total"], row["stock_count"], row["metrics_json"]),
            )

        # 成分股：当日全量重写
        cursor.execute("DELETE FROM board_component WHERE snapshot_date = ?", (d,))
        component_rows = 0
        for code, sector_keys in snap.stock_to_sectors.items():
            for sk in sector_keys:
                sector = snap.sectors.get(sk)
                name = sector.name if sector else sk
                cursor.execute(
                    "INSERT INTO board_component "
                    "(snapshot_date, sector_key, sector_name, stock_code) "
                    "VALUES (?, ?, ?, ?)",
                    (d, sk, name, code),
                )
                component_rows += 1

        conn.commit()
        snap.component_count = component_rows
        logger.info("板块快照已落库 %s: %d 个板块, %d 条成分股归属",
                    d, len(snap.sectors), component_rows)

    # ---------------- 清理 ----------------

    def prune(self, retention_days: int) -> int:
        """清理早于保留窗口的快照（绝不删当天）。返回删除的快照日期数"""
        today = date.today()
        cutoff = (today - timedelta(days=retention_days)).isoformat()
        conn = self._get_conn()
        cursor = conn.cursor()
        deleted_dates = cursor.execute(
            "SELECT DISTINCT snapshot_date FROM board_snapshot "
            "WHERE snapshot_date < ?", (cutoff,),
        ).fetchall()
        cursor.execute("DELETE FROM board_snapshot WHERE snapshot_date < ?", (cutoff,))
        cursor.execute("DELETE FROM board_component WHERE snapshot_date < ?", (cutoff,))
        conn.commit()
        if deleted_dates:
            logger.info("板块快照清理: 删除 %d 天(早于 %s)",
                        len(deleted_dates), cutoff)
        return len(deleted_dates)

    # ---------------- 快照清单（--check 用） ----------------

    def list_dates(self, limit: int = 10) -> List[dict]:
        """列出最近快照日期及规模，供 --check 诊断"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT snapshot_date, COUNT(*) AS sector_count, "
            "       SUM(stock_count) AS stock_total "
            "FROM board_snapshot GROUP BY snapshot_date "
            "ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"snapshot_date": r["snapshot_date"],
             "sector_count": r["sector_count"],
             "stock_total": r["stock_total"] or 0}
            for r in rows
        ]
