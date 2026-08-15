"""
数据库初始化与管理
SQLite + WAL模式，支持并发读取

P1-13: 连接池优化（thread-local 复用连接）
原：每次 get_connection() 新建连接，close() 释放
新：thread-local 缓存连接，同一线程复用，避免重复创建
"""
import sqlite3
import os
import threading
from pathlib import Path
from datetime import date
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "stock_agent.db"

# P1-13: thread-local 连接池
_thread_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """
    获取数据库连接，启用WAL模式

    P1-13: thread-local 复用连接
    同一线程多次调用返回同一连接，避免重复创建/关闭

    P1-13 补充：若线程缓存连接已被历史方法显式 close()，自动重建，避免返回已关闭连接。
    """
    conn = getattr(_thread_local, 'conn', None)
    if conn is None:
        conn = _open_connection()
        _thread_local.conn = conn
        return conn
    try:
        conn.execute("SELECT 1")
    except (sqlite3.ProgrammingError, sqlite3.OperationalError):
        conn = _open_connection()
        _thread_local.conn = conn
    return conn


def _open_connection() -> sqlite3.Connection:
    """新建一条 WAL 连接（线程内唯一）"""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    """
    P1-13: 上下文管理器（推荐使用方式）
    with get_conn() as conn:
        conn.execute(...)
    """
    conn = get_connection()
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        raise
    # 不 close（thread-local 复用）


def close_thread_connection():
    """关闭当前线程的连接（线程结束时调用）"""
    conn = getattr(_thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None


def init_db():
    """初始化数据库表"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()

    # 观点主表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS insights (
        id TEXT PRIMARY KEY,
        source TEXT,
        raw_text TEXT,
        created_at DATE,
        status TEXT DEFAULT 'tracking',
        updated_at DATE
    )
    """)

    # 判断表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judgments (
        id TEXT PRIMARY KEY,
        insight_id TEXT REFERENCES insights(id),
        judgment TEXT NOT NULL,
        direction TEXT,
        time_horizon TEXT,
        confidence TEXT,
        valid_days INTEGER,
        expire_at DATE,
        status TEXT DEFAULT 'tracking',
        refute_count INTEGER DEFAULT 0,
        tags TEXT,
        verify_config TEXT,
        created_at DATE,
        updated_at DATE
    )
    """)

    # 判断关联标的表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judgment_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judgment_id TEXT REFERENCES judgments(id),
        stock_code TEXT,
        stock_name TEXT,
        role TEXT,
        profile_json TEXT,
        suggested_to_watchlist BOOLEAN DEFAULT FALSE,
        created_at DATE
    )
    """)

    # 判断关联链表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judgment_chains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        upstream_judgment_id TEXT REFERENCES judgments(id),
        downstream_judgment_id TEXT REFERENCES judgments(id),
        logic TEXT
    )
    """)

    # 追踪指标每日快照
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS insight_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judgment_id TEXT REFERENCES judgments(id),
        stock_code TEXT,
        date DATE,
        metric_name TEXT,
        metric_value TEXT,
        note TEXT
    )
    """)

    # 交易日志
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trade_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL,
        time TIME,
        stock_code TEXT,
        stock_name TEXT,
        signal_type TEXT,
        entry_type TEXT,
        exit_type TEXT,
        trigger_price REAL,
        shares REAL,
        stop_loss REAL,
        target_price REAL,
        suggested_position REAL,
        mode_at_signal TEXT,
        sector_status TEXT,
        market_score INTEGER,
        user_action TEXT,
        actual_price REAL,
        actual_position REAL,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 数据缓存表（避免重复调用API）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS data_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_key TEXT UNIQUE NOT NULL,
        cache_value TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expire_at TIMESTAMP
    )
    """)

    # 大盘评分历史
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS market_score_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL,
        score REAL NOT NULL,
        mode TEXT NOT NULL,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 板块扫描历史
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sector_scan_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL,
        sector_name TEXT NOT NULL,
        classification TEXT NOT NULL,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    _migrate()
    logger.info("Database initialized successfully at %s", DB_PATH)


def _migrate():
    """增量迁移：为已存在的表补充新增列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）"""
    conn = get_connection()
    cursor = conn.cursor()
    # trade_logs.shares（P1-3 反馈闭环：记录建议股数，供 executed 后聚合持仓）
    try:
        cols = {r[1] for r in cursor.execute("PRAGMA table_info(trade_logs)")}
        if "shares" not in cols:
            cursor.execute("ALTER TABLE trade_logs ADD COLUMN shares REAL")
            conn.commit()
            logger.info("迁移: trade_logs 新增 shares 列")
    except Exception as e:
        logger.error("迁移 trade_logs.shares 失败: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
