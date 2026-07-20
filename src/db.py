"""
数据库初始化与管理
SQLite + WAL模式，支持并发读取
"""
import sqlite3
import os
from pathlib import Path
from datetime import date
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "stock_agent.db"


def get_connection() -> sqlite3.Connection:
    """获取数据库连接，启用WAL模式"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
    conn.close()
    logger.info("Database initialized successfully at %s", DB_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
