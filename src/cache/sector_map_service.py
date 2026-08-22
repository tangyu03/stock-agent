"""
板块映射服务编排层（Step1 / Step2 统一入口）

Step1 build_snapshot — 板块层面构建按天快照：
  排名(东财push2) → 并发成分股 → K线轻量分类 → 合并 BoardSnapshot → 落库 → 回填内存

Step2 classify_stocks — 个股层面分类（纯读快照，0 成分股 API、0 K线分类）：
  内存 → SQLite 当天 → 最近一天(stale) → 归属反查 × 当日实时排名合并

惰性重建：ensure_snapshot 当天快照缺失时现场构建（进程内锁 + SQLite 侧构建标记防多进程并发）。
降级链：快照缺失 → 惰性重建 → 失败由调用方（sector_ranker.classify_stocks）走旧路径。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .board_snapshot import BoardSnapshot, BoardSector
from .snapshot_store import SnapshotStore
from .ttl_cache import MemoryTTLCache
from . import builders

logger = logging.getLogger(__name__)

# 内存缓存键
_KEY_SNAPSHOT = "snapshot:{date}"
_KEY_STOCK_SECTORS = "stock_sectors:{date}"

# 跨进程构建锁标记（data_cache 表）
_BUILD_LOCK_KEY = "sector_map_building:{date}"
_BUILD_LOCK_TTL_SECONDS = 30 * 60


def _today() -> str:
    return date.today().isoformat()


class SectorMapService:
    def __init__(self, config: dict = None, store: SnapshotStore = None,
                 mem_cache: MemoryTTLCache = None):
        cfg = (config or {}).get("sector_map", {}) or {}
        self.cfg = cfg
        self.store = store or SnapshotStore()
        self.mem = mem_cache or MemoryTTLCache(
            default_ttl_seconds=int(cfg.get("cache", {}).get("memory_ttl_seconds", 300))
        )
        self._build_lock = threading.Lock()

    # ---------------- 配置取值 ----------------

    def _max_workers(self) -> int:
        return int(self.cfg.get("max_workers", 8))

    def _fetch_components(self) -> bool:
        return bool(self.cfg.get("fetch_components", True))

    def _classify_by(self) -> str:
        return str(self.cfg.get("classify_by", "percentile"))

    def _stale_max_days(self) -> int:
        return int(self.cfg.get("stale_max_days", 5))

    def _retention_days(self) -> int:
        return int(self.cfg.get("snapshot_retention_days", 10))

    def _lazy_rebuild(self) -> bool:
        return bool(self.cfg.get("lazy_rebuild", True))

    def _force_fresh(self) -> bool:
        return bool(self.cfg.get("force_fresh", False))

    # ---------------- 板块列表展开 ----------------

    def _expand_industries(self) -> Dict[str, str]:
        """按 scope 展开 {sector_key: 板块名}。默认全扫 THS 90 行业。"""
        from ..data_layer.sw_industry import _load_ths_industries, THS_INDUSTRIES
        _load_ths_industries()
        scope = self.cfg.get("scope", "all")

        if isinstance(scope, list):
            result = {}
            for name in scope:
                code = _normalize_to_key(name)
                if code:
                    result[code] = THS_INDUSTRIES.get(code, name) if code in THS_INDUSTRIES else name
            return result

        if scope == "watchlist":
            return self._expand_watchlist_industries()

        # all：全扫 90 行业
        return dict(THS_INDUSTRIES)

    def _expand_watchlist_industries(self) -> Dict[str, str]:
        """watchlist 模式：反查 30 只自选所属板块，只扫这些"""
        from ..config_models import load_config
        from ..data_layer.sw_industry import fetch_stock_sector, _load_ths_industries, THS_INDUSTRIES
        _load_ths_industries()
        portfolio = load_config("portfolio.yaml")
        codes = [s.get("code", "") for s in (portfolio.get("stocks") or []) if s.get("code")]
        result = {}
        for c in codes:
            name = fetch_stock_sector(c)
            if name and name in THS_INDUSTRIES.values():
                code = next((k for k, v in THS_INDUSTRIES.items() if v == name), None)
                if code:
                    result[code] = name
        if not result:
            logger.warning("watchlist 模式反查为空，回退全扫 90 行业")
            return dict(THS_INDUSTRIES)
        return result

    # ---------------- Step1：构建 ----------------

    def build_snapshot(self, trade_date: Optional[str] = None, force: bool = False) -> BoardSnapshot:
        """
        Step1 板块层面构建按天快照。

        流程：排名(1次API) → 并发成分股 → K线轻量分类 → 合并落库 → 回填内存 → 清理过期
        """
        d = _today() if trade_date is None else str(trade_date)[:10]

        if not force and self.store.has(d):
            logger.info("板块快照 %s 已存在，跳过构建（force=False）", d)
            snap, _ = self.store.latest(self._stale_max_days())
            return snap

        logger.info("===== Step1 板块快照构建 %s 开始 =====", d)
        t0 = time.time()

        # 1. 板块排名（东财 push2，1 次 API）
        ranking = builders.fetch_eastmoney_ranking(
            retries=int(self.cfg.get("api", {}).get("ranking_retries", 10)),
        )
        if ranking is None:
            ranking = {}

        # 2. 展开板块列表
        industries = self._expand_industries()
        if not industries:
            raise RuntimeError("板块列表为空，无法构建快照")

        # 3. 成分股/归属：
        #    主来源 = datacenter 全量 A 股行业归属（复用 build_sector_mapping.py 数据源，不反爬，1 次拉取）
        #    补缺   = cons_em 逐板块拉取（只对 datacenter 未覆盖的板块，可配置开关）
        stock_to_sectors: Dict[str, List[str]] = {}
        if self._fetch_components():
            industry_map = builders.fetch_all_stock_industry_map()
            stock_to_sectors = builders.build_stock_to_sectors_from_industry_map(industry_map)

            if self.cfg.get("cons_em_fallback", True):
                covered = set(k for keys in stock_to_sectors.values() for k in keys)
                missing = {k: n for k, n in industries.items() if k not in covered}
                if missing:
                    logger.info("cons_em 补缺: datacenter 未覆盖 %d 个板块", len(missing))
                    comp = builders.fetch_components_concurrent(
                        missing,
                        max_workers=self._max_workers(),
                        per_timeout=float(self.cfg.get("api", {}).get("cons_timeout", 30)),
                    )
                    for key, codes in comp.items():
                        for c in codes:
                            stock_to_sectors.setdefault(c, []).append(key)

        # 4. K线轻量指标 + 分类（主线程串行，纯计算 + ths_cache 读）
        classify_by = self._classify_by()
        sectors: Dict[str, BoardSector] = {}
        # 每板块成分股数（从反查索引统计）
        sector_stock_count: Dict[str, int] = {}
        for keys in stock_to_sectors.values():
            for k in keys:
                sector_stock_count[k] = sector_stock_count.get(k, 0) + 1
        percentile = builders.classify_by_percentile(ranking)

        for key, name in industries.items():
            # 排名（东财名）与 THS 名做匹配：精确 → 包含
            rank_info = None
            if name in ranking:
                rank_info = ranking[name]
            else:
                for rname, rinfo in ranking.items():
                    if len(name) >= 2 and (name in rname or rname in name):
                        rank_info = rinfo
                        break

            metrics = builders.compute_sector_metrics_from_kline(name)

            if classify_by == "metrics":
                classification = builders.classify_sector_status(metrics)
            elif rank_info is not None:
                classification = rank_info.get("classification", "rotational")
            else:
                # 排名未命中：用 K 线规则兜底
                classification = builders.classify_sector_status(metrics)

            sectors[key] = BoardSector(
                sector_key=key,
                name=name,
                source="THS",
                classification=classification,
                change_pct=rank_info.get("change_pct", 0.0) if rank_info else metrics.get("change_3d", 0.0),
                rank=rank_info.get("rank", 0) if rank_info else 0,
                total=len(industries),
                stock_count=sector_stock_count.get(key, 0),
                metrics=metrics,
            )

        snap = BoardSnapshot(
            snapshot_date=d,
            trade_date=d,
            created_at=datetime.now().isoformat(timespec="seconds"),
            sectors=sectors,
            stock_to_sectors=stock_to_sectors,
        )

        # 4. 落库 + 清理 + 回填内存
        self.store.save(snap)
        self.store.prune(self._retention_days())
        self.mem.set(_KEY_SNAPSHOT.format(date=d), snap, ttl_seconds=0)
        self.mem.set(_KEY_STOCK_SECTORS.format(date=d), stock_to_sectors, ttl_seconds=0)

        logger.info("===== Step1 板块快照构建完成 %s: %d 板块, %d 只成分股, 耗时 %.1fs =====",
                    d, len(sectors), len(stock_to_sectors), time.time() - t0)
        return snap

    # ---------------- 读取（内存 → SQLite 当天 → 最近 stale） ----------------

    def get_snapshot(self) -> Tuple[Optional[BoardSnapshot], int]:
        """
        取快照：内存 → SQLite 当天 → SQLite 最近一天。
        Returns:
            (快照, 滞后天数)：滞后 0 = 当天新鲜，>0 = stale；None = 无可用
        """
        today = _today()

        # 1. 内存
        snap = self.mem.get(_KEY_SNAPSHOT.format(date=today))
        if snap is not None:
            return snap, 0

        # 2. SQLite 当天
        if self.store.has(today):
            snap = self.store.load(today)
            if snap is not None:
                self.mem.set(_KEY_SNAPSHOT.format(date=today), snap, ttl_seconds=0)
                return snap, 0

        # 3. 最近一天（跨日 stale 降级可用）
        snap, lag = self.store.latest(self._stale_max_days())
        if snap is not None and lag > 0:
            snap.stale = True
            logger.warning("板块快照 %s 非当天（滞后 %d 天），使用 stale 降级",
                           snap.snapshot_date, lag)
        return snap, lag

    def ensure_snapshot(self, force: bool = False) -> Tuple[Optional[BoardSnapshot], int]:
        """
        确保有可用快照：当天缺失 → 现场重建一次（带锁 + 跨进程标记）。
        Returns:
            (快照, 滞后天数)；重建失败返回 (None, 0) 交给调用方降级
        """
        snap, lag = self.get_snapshot()
        if snap is not None and lag == 0 and not force:
            return snap, lag

        # 跨日 stale 且未强制新鲜：降级可用（已确认决策），不触发重建
        if snap is not None and not self._force_fresh():
            return snap, lag

        if not self._lazy_rebuild():
            return snap, lag

        today = _today()
        with self._build_lock:
            # 双检：拿锁后可能已被其他线程构建
            if self.store.has(today) and not force:
                snap2 = self.store.load(today)
                if snap2 is not None:
                    return snap2, 0

            # 跨进程标记：防止主流程与定时脚本同时重建
            if self._is_building(today):
                logger.warning("板块快照 %s 正在其他进程构建，跳过本进程重建", today)
                return snap, lag
            self._mark_building(today)

            try:
                built = self.build_snapshot(today, force=force)
                return built, 0
            except Exception as e:
                logger.warning("惰性重建板块快照失败: %s", str(e)[:120])
                return None, 0
            finally:
                self._clear_building(today)

    # ---------------- 跨进程构建标记 ----------------

    def _is_building(self, d: str) -> bool:
        try:
            from ..db import get_connection
            conn = get_connection()
            row = conn.execute(
                "SELECT 1 FROM data_cache WHERE cache_key = ? AND expire_at > datetime('now')",
                (_BUILD_LOCK_KEY.format(date=d),),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _mark_building(self, d: str) -> None:
        try:
            import json
            from ..db import get_connection
            conn = get_connection()
            expiry = (datetime.now() + timedelta(seconds=_BUILD_LOCK_TTL_SECONDS)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) "
                "VALUES (?, ?, ?)",
                (_BUILD_LOCK_KEY.format(date=d), json.dumps({"ts": time.time()}), expiry),
            )
            conn.commit()
        except Exception:
            pass

    def _clear_building(self, d: str) -> None:
        try:
            from ..db import get_connection
            conn = get_connection()
            conn.execute("DELETE FROM data_cache WHERE cache_key = ?",
                         (_BUILD_LOCK_KEY.format(date=d),))
            conn.commit()
        except Exception:
            pass

    # ---------------- Step2：个股分类（纯读快照） ----------------

    def classify_stocks(self, stock_codes: List[str]) -> Dict[str, dict]:
        """
        Step2 个股分类主入口：只读快照，0 成分股 API、0 K线分类。

        快照缺失/失败时返回空 dict，由调用方（sector_ranker.classify_stocks）走降级链。
        """
        snap, lag = self.get_snapshot()
        if snap is None:
            logger.info("sector_ranker: 无板块快照可用，等待惰性重建/降级")
            return {}
        return self._classify_from_snapshot(stock_codes, snap, lag)

    def _classify_from_snapshot(self, codes: List[str], snap: BoardSnapshot,
                                stale_days: int) -> Dict[str, dict]:
        """从快照分类：归属反查 × 当日实时排名合并（排名失败用快照内 rank/classification）"""
        today = _today()

        # 1. 归属反查：内存 → SQLite 按需查
        stock_sectors = self.mem.get(_KEY_STOCK_SECTORS.format(date=snap.snapshot_date))
        if stock_sectors is None:
            stock_sectors = self.store.load_stock_sectors(snap.snapshot_date, codes)
            self.mem.set(_KEY_STOCK_SECTORS.format(date=snap.snapshot_date),
                         stock_sectors, ttl_seconds=0)

        # 2. 当日实时排名（唯一可刷新项，保证盘中新鲜；最多 1 次 API）
        ranking = None
        try:
            from ..analyzers.sector_ranker import _refresh_daily_ranking
            ranking = _refresh_daily_ranking()
        except Exception:
            ranking = None

        result: Dict[str, dict] = {}
        hit_count = 0
        for code in codes:
            sector_keys = stock_sectors.get(code, [])
            if not sector_keys:
                result[code] = {
                    "classification": "unknown",
                    "sectors": [],
                    "best_sector": None,
                }
                continue

            hit_count += 1
            # 收集该股所有板块（用当日排名覆盖涨跌幅/分类/排名）
            sector_entries = []
            for sk in sector_keys:
                sec = snap.sectors.get(sk)
                if sec is None:
                    continue
                name = sec.name
                entry = {
                    "type": sec.source,
                    "name": name,
                    "change_pct": sec.change_pct,
                    "classification": sec.classification,
                    "rank": sec.rank,
                }
                # 当日排名覆盖（板块名精确/部分匹配，逻辑同 _lookup_from_cache_table）
                if ranking:
                    daily = ranking.get(name)
                    if not daily:
                        for rname, rinfo in ranking.items():
                            if len(name) >= 2 and (name in rname or rname in name):
                                daily = rinfo
                                break
                    if daily:
                        entry["change_pct"] = daily.get("change_pct", entry["change_pct"])
                        entry["classification"] = daily.get("classification", entry["classification"])
                        entry["rank"] = daily.get("rank", entry["rank"])
                sector_entries.append(entry)

            if not sector_entries:
                result[code] = {
                    "classification": "unknown",
                    "sectors": [],
                    "best_sector": None,
                }
                continue

            # 最严格标签：退潮 > 主线 > 轮动（与 sector_ranker 语义一致）
            best = None
            for s in sector_entries:
                if s["classification"] == "retreating":
                    best = s
                    break
                elif s["classification"] == "main_trend" and best is None:
                    best = s
            if best is None:
                best = sector_entries[0]

            result[code] = {
                "classification": best.get("classification", "rotational"),
                "sectors": sector_entries,
                "best_sector": best,
            }

        logger.info("sector_ranker: 从快照分类 %d/%d 只 (快照 %s%s)",
                    hit_count, len(codes), snap.snapshot_date,
                    ", stale" if snap.stale else "")
        return result


# 模块级单例（进程内复用，避免每次重建配置/连接）
_service_instance: Optional[SectorMapService] = None
_service_lock = threading.Lock()


def get_sector_map_service() -> SectorMapService:
    """获取进程内单例"""
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                from ..config_models import load_config
                try:
                    config = load_config("sector_map.yaml")
                except Exception:
                    config = {}
                _service_instance = SectorMapService(config=config)
    return _service_instance


def _normalize_to_key(name: str) -> Optional[str]:
    """板块名 → THS 代码；无法识别时返回名称本身（非 THS 板块用名称作 key）"""
    try:
        from ..data_layer.sw_industry import normalize_sector, _load_ths_industries, THS_INDUSTRIES
        _load_ths_industries()
        code = normalize_sector(name)
        if code:
            return code
        if name in THS_INDUSTRIES.values():
            return next((k for k, v in THS_INDUSTRIES.items() if v == name), None)
        return name
    except Exception:
        return name
