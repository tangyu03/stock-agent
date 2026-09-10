"""Intercepted-observation tracking (P3-2)."""

import re
import statistics
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional

from ..db import get_conn


CHECKPOINTS = ("T+5", "T+10", "T+20")


def _ensure_table(cursor) -> None:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS observation_intercepts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracking_key TEXT UNIQUE NOT NULL,
        track_date TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        tech_score REAL,
        intercept_reason TEXT,
        t0_close REAL NOT NULL,
        benchmark_close REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_observation_intercepts_date_code "
        "ON observation_intercepts(track_date, stock_code)"
    )
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS observation_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracking_id INTEGER NOT NULL REFERENCES observation_intercepts(id),
        checkpoint TEXT NOT NULL,
        checkpoint_date TEXT NOT NULL,
        close_price REAL NOT NULL,
        benchmark_close REAL,
        excess_return REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tracking_id, checkpoint)
    )
    """)


def _parse_tech_score(diagnostic: str) -> Optional[float]:
    match = re.search(r"评分[:：]\s*(-?\d+(?:\.\d+)?)(?:/6)?", str(diagnostic or ""))
    return float(match.group(1)) if match else None


def record_observation_t0(
    observations: Iterable[Dict],
    track_date: str,
    benchmark_close: Optional[float] = None,
) -> int:
    """Record one T0 per code/day; duplicate intraday reports are ignored."""
    inserted = 0
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        for item in observations or []:
            code = str(item.get("stock_code", "")).strip()
            try:
                close = float(item.get("current_price") or 0)
            except (TypeError, ValueError):
                close = 0.0
            if not code or close <= 0:
                continue
            diagnostic = str(item.get("entry_diagnostic") or item.get("diagnostic") or "")
            cursor.execute(
                """
                INSERT OR IGNORE INTO observation_intercepts
                (tracking_key, track_date, stock_code, stock_name, tech_score,
                 intercept_reason, t0_close, benchmark_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{track_date}:{code}", track_date, code,
                    str(item.get("stock_name") or code),
                    _parse_tech_score(diagnostic),
                    str(item.get("intercept_reason") or item.get("note") or ""),
                    close,
                    float(benchmark_close) if benchmark_close else None,
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
    return inserted


def get_observation_intercepts(
    track_date: Optional[str] = None,
    code: str = "",
) -> List[Dict]:
    clauses = []
    params: List = []
    if track_date:
        clauses.append("track_date = ?")
        params.append(track_date)
    if code:
        clauses.append("stock_code = ?")
        params.append(code)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            f"SELECT * FROM observation_intercepts{where} ORDER BY track_date, stock_code",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


def _business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    days = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def _due_date(track_date: str, offset: int, as_of: date) -> Optional[str]:
    try:
        start = date.fromisoformat(track_date)
    except ValueError:
        return None
    cursor = start + timedelta(days=1)
    seen = 0
    while cursor <= as_of:
        if cursor.weekday() < 5:
            seen += 1
            if seen == offset:
                return cursor.isoformat()
        cursor += timedelta(days=1)
    return None


def _required_start(row: Dict) -> str:
    try:
        return (date.fromisoformat(row["track_date"]) - timedelta(days=10)).isoformat()
    except (KeyError, ValueError):
        return row["track_date"]


def fill_observation_checkpoints(
    as_of: Optional[date] = None,
    price_provider: Optional[Callable[[str, str], Optional[float]]] = None,
    benchmark_provider: Optional[Callable[[str], Optional[float]]] = None,
) -> Dict[str, int]:
    """Fill due checkpoints. A missing price is left unfilled, not fabricated."""
    as_of = as_of or date.today()
    if price_provider is None:
        def price_provider(code: str, checkpoint_date: str) -> Optional[float]:
            from ..loop.data_loader import DataLoader
            kline = DataLoader().load_kline([code], checkpoint_date, checkpoint_date)
            rows = kline.get(code) or []
            return float(rows[0]["close"]) if rows and rows[0].get("close") else None

    if benchmark_provider is None:
        def benchmark_provider(checkpoint_date: str) -> Optional[float]:
            from ..data_layer.akshare_adapter import get_akshare_adapter
            result = get_akshare_adapter().get_index_data("000300")
            if not result.success:
                return None
            for row in result.data or []:
                row_date = str(row.get("date") or row.get("日期") or "")[:10]
                if row_date == checkpoint_date:
                    return float(row.get("close") or row.get("收盘") or 0) or None
            return None

    filled = 0
    missing = 0
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            """
            SELECT o.* FROM observation_intercepts o
            WHERE NOT EXISTS (
                SELECT 1 FROM observation_checkpoints c
                WHERE c.tracking_id = o.id AND c.checkpoint = ?
            )
            ORDER BY o.track_date, o.stock_code
            """,
            (CHECKPOINTS[0],),
        )
        pending = [dict(row) for row in cursor.fetchall()]
    for row in pending:
        for index, checkpoint in enumerate(CHECKPOINTS, 1):
            existing = _checkpoint(row["id"], checkpoint)
            if existing:
                continue
            checkpoint_date = _due_date(row["track_date"], index, as_of)
            if not checkpoint_date:
                continue
            try:
                close = float(price_provider(row["stock_code"], checkpoint_date) or 0)
            except Exception:
                close = 0.0
            if close <= 0:
                missing += 1
                continue
            try:
                benchmark = benchmark_provider(checkpoint_date)
            except Exception:
                benchmark = None
            excess = None
            t0_close = float(row["t0_close"] or 0)
            t0_benchmark = float(row["benchmark_close"] or 0)
            if t0_close > 0 and benchmark and t0_benchmark > 0:
                excess = (
                    (close / t0_close - 1.0)
                    - (benchmark / t0_benchmark - 1.0)
                ) * 100
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_table(cursor)
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO observation_checkpoints
                    (tracking_id, checkpoint, checkpoint_date, close_price,
                     benchmark_close, excess_return)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], checkpoint, checkpoint_date, close,
                        benchmark, excess,
                    ),
                )
                filled += cursor.rowcount
            # Checkpoints are sequential; stop when the current one is still missing.
            if close <= 0:
                break
    return {"filled": filled, "missing": missing}


def _checkpoint(tracking_id: int, checkpoint: str) -> Optional[Dict]:
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            "SELECT * FROM observation_checkpoints WHERE tracking_id=? AND checkpoint=?",
            (tracking_id, checkpoint),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def build_intercept_weekly_summary(rows: Optional[List[Dict]] = None) -> str:
    """Weekly layered summary over all checkpoint rows, including removed stocks."""
    if rows is None:
        with get_conn() as conn:
            cursor = conn.cursor()
            _ensure_table(cursor)
            cursor.execute("""
                SELECT o.tech_score, c.checkpoint, c.excess_return
                FROM observation_intercepts o
                JOIN observation_checkpoints c ON c.tracking_id = o.id
                ORDER BY c.checkpoint_date
            """)
            rows = [dict(row) for row in cursor.fetchall()]
    if not rows:
        return "拦截票追踪: 样本0；T+5中位超额未积累"
    sample = len(rows)
    t5 = [
        float(r["excess_return"]) for r in rows
        if r.get("checkpoint") == "T+5" and r.get("excess_return") is not None
    ]
    median = statistics.median(t5) if t5 else None
    groups = {"strong": [], "weak": []}
    for r in rows:
        if r.get("tech_score") is None or r.get("excess_return") is None:
            continue
        score = float(r["tech_score"])
        if score >= 1:
            groups["strong"].append(float(r["excess_return"]))
        elif score <= -1:
            groups["weak"].append(float(r["excess_return"]))
    median_text = f"{median:+.2f}%" if median is not None else "未回填"
    strong_text = (
        f"{statistics.median(groups['strong']):+.2f}%" if groups["strong"] else "未积累"
    )
    weak_text = (
        f"{statistics.median(groups['weak']):+.2f}%" if groups["weak"] else "未积累"
    )
    return (
        f"拦截票追踪: 累计样本{sample} | T+5中位超额{median_text} | "
        f"分层: 技术分≥+1组{strong_text} / ≤-1组{weak_text}"
    )
