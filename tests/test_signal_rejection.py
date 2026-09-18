"""信号被拒独立模块测试（signal_rejection 表）"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.analyzers.signal_rejection import (
    RejectionLedger,
    persist_rejection,
    query_rejections,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rejection.db")
    if hasattr(db._thread_local, "conn"):
        try:
            db._thread_local.conn.close()
        except Exception:
            pass
        db._thread_local.conn = None
    db.init_db()
    yield db
    if hasattr(db._thread_local, "conn"):
        try:
            db._thread_local.conn.close()
        except Exception:
            pass
        db._thread_local.conn = None


def test_ledger_record_pop_clear():
    ledger = RejectionLedger()
    ledger.record("603061", {"stock_code": "603061", "entry_type": "价量突破", "reasons": ["a"]})
    ledger.record("000001", {"stock_code": "000001", "entry_type": "再入场上限", "reasons": ["b"]})
    assert ledger.pending["603061"]["entry_type"] == "价量突破"
    popped = ledger.pop("603061")
    assert popped["reasons"] == ["a"]
    assert "603061" not in ledger.pending
    ledger.clear()
    assert ledger.pending == {}


def test_persist_and_query(tmp_db):
    rid = persist_rejection({
        "stock_code": "603061",
        "stock_name": "金海通",
        "entry_type": "价量突破",
        "reasons": ["止损缓冲不足(Z宽度)", "估值透镜: 低基数反转"],
        "benchmark_price": 328.54,
        "stop_loss": 306.86,
        "target_range": [375.68, 400.03],
        "hypothesis": {"x": "突破MA25", "y": 328.54, "z": 306.86, "w": [375.68, 400.03]},
        "fundamental_rejected": False,
        "valuation_rejected": True,
    })
    assert rid is not None

    rows = query_rejections(stock_code="603061")
    assert len(rows) == 1
    row = rows[0]
    assert row["stock_code"] == "603061"
    assert row["stock_name"] == "金海通"
    assert "止损缓冲不足" in row["reason"]
    assert row["missing_fields"] == ""
    assert row["detail"]["benchmark_price"] == 328.54
    assert row["detail"]["valuation_rejected"] is True


def test_persist_missing_hypothesis_fields(tmp_db):
    rid = persist_rejection({
        "stock_code": "603061",
        "stock_name": "金海通",
        "entry_type": "价量突破",
        "reasons": ["缺 Y"],
        "hypothesis": {"x": "only x"},
    })
    assert rid is not None
    rows = query_rejections(stock_code="603061")
    assert rows[0]["missing_fields"] == "Y,Z,W"


def test_persist_budget_blocked_schema(tmp_db):
    # 预算拦截的 dict 只有 reason（字符串）没有 reasons list
    rid = persist_rejection({
        "stock_code": "603061",
        "stock_name": "金海通",
        "entry_type": "价量突破",
        "reason": "组合预算:板块[半导体设备]并发敞口已达2/2",
    })
    assert rid is not None
    rows = query_rejections(stock_code="603061")
    assert "组合预算" in rows[0]["reason"]
