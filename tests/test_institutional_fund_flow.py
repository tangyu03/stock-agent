import time

import importlib
import akshare
import pandas as pd

import src.analyzers.institutional_scorer as inst
from src.data_layer import iwencai_api


def setup_function():
    inst._reset_institutional_state()


def test_empty_iwencai_response_does_not_trip_global_breaker(monkeypatch):
    monkeypatch.setattr(iwencai_api, "_call_api", lambda **kwargs: None)

    def fallback(code):
        return {
            "vote": 0,
            "detail": "fallback ok",
            "raw": {"net_flows": [1.0, 2.0, 3.0], "code": code},
        }

    monkeypatch.setattr(inst, "_fetch_main_force_flow_fallback", fallback)

    first = inst._fetch_main_force_flow("000001")
    second = inst._fetch_main_force_flow("000002")

    assert first["detail"] == "fallback ok"
    assert second["raw"]["code"] == "000002"
    assert not inst._api_disabled["main_force"]
    assert inst._api_fail_count["main_force"] == 0


def test_only_failed_stock_is_short_circuited(monkeypatch):
    inst._fund_flow_rank_last_error = ""
    monkeypatch.setattr(iwencai_api, "_call_api", lambda **kwargs: None)

    def fallback(code):
        return None if code == "000001" else {
            "vote": 1,
            "detail": "healthy",
            "raw": {"net_flows": [1.0]},
        }

    monkeypatch.setattr(inst, "_fetch_main_force_flow_fallback", fallback)

    inst._fetch_main_force_flow("000001")
    inst._fetch_main_force_flow("000001")
    inst._fetch_main_force_flow("000002")

    assert inst._main_force_failures["000001"] == 1
    assert inst._main_force_failures.get("000002") is None
    assert not inst._api_disabled["main_force"]


def test_short_iwencai_series_uses_fallback_series(monkeypatch):
    monkeypatch.setattr(
        iwencai_api,
        "_call_api",
        lambda **kwargs: {"datas": [{"main_force_flow[20260829]": 100}]},
    )
    monkeypatch.setattr(
        inst,
        "_fetch_main_force_flow_fallback",
        lambda code: {
            "vote": -1,
            "detail": "fallback outflow",
            "raw": {"net_flows": [-100.0, -200.0, -300.0]},
        },
    )

    result = inst._fetch_main_force_flow("000001")

    assert result["vote"] == -1
    assert result["detail"] == "fallback outflow"
    assert not inst._api_disabled["main_force"]


def test_batch_snapshot_is_parsed_and_cached(monkeypatch):
    frame = pd.DataFrame(
        {
            "代码": ["600000", "000001", "300750"],
            "3日主力净流入-净额": ["1.2e8", -3.0e8, 0.0],
        }
    )
    calls = []

    def fake_retry(func, **kwargs):
        calls.append(kwargs)
        return frame

    monkeypatch.setattr(inst, "call_ak_with_retry", fake_retry)

    first = inst._fetch_main_force_flow_fallback("600000")
    second = inst._fetch_main_force_flow_fallback("000001")
    third = inst._fetch_main_force_flow_fallback("300750")

    assert first["vote"] == 1
    assert first["raw"]["total"] == 120000000.0
    assert second["vote"] == -1
    assert third["vote"] == 0
    assert len(calls) == 1
    assert len(inst._fund_flow_rank_cache) == 3


def test_ths_snapshot_is_used_when_eastmoney_fails(monkeypatch):
    frame = pd.DataFrame(
        {
            "股票代码": ["600000", 2084],
            "资金流入净额": ["1.04亿", "-1533.52万"],
        }
    )

    def fake_retry(func, **kwargs):
        if func.__name__ == "stock_individual_fund_flow_rank":
            return None
        return frame

    monkeypatch.setattr(inst, "call_ak_with_retry", fake_retry)

    first = inst._fetch_main_force_flow_fallback("600000")
    second = inst._fetch_main_force_flow_fallback("002084")

    assert inst._fund_flow_rank_source == "ths"
    assert first["vote"] == 1
    assert first["raw"]["total"] == 104000000.0
    assert second["vote"] == -1
    assert second["raw"]["total"] == -15335200.0


def test_batch_snapshot_backs_off_instead_of_fixed_cooldown(monkeypatch):
    calls = []

    def raise_remote_disconnected(**kwargs):
        calls.append(kwargs)
        raise Exception(
            "('Connection aborted.', RemoteDisconnected("
            "'Remote end closed connection'))"
        )

    def fake_retry(func, **kwargs):
        try:
            return func(**kwargs)
        except Exception:
            return None

    monkeypatch.setattr(
        akshare, "stock_individual_fund_flow_rank", raise_remote_disconnected
    )
    monkeypatch.setattr(
        akshare, "stock_fund_flow_individual", raise_remote_disconnected
    )
    monkeypatch.setattr(inst, "call_ak_with_retry", fake_retry)

    assert inst._fetch_main_force_flow_fallback("600000") is None
    assert inst._fetch_main_force_flow_fallback("000001") is None

    assert len(calls) == 2
    assert inst._main_force_fallback_failures == 1
    assert inst._main_force_fallback_block_until > time.monotonic()


def test_bse_missing_data_neutralizes_bullish_votes(monkeypatch):
    # test_timing_engine replaces this module function at import time; reload
    # restores the real scorer so the downweight rule is tested directly.
    importlib.reload(inst)
    monkeypatch.setattr(
        inst, "_fetch_margin_balance",
        lambda code: {"vote": 1, "detail": "bullish", "raw": {}},
    )
    monkeypatch.setattr(
        inst, "_fetch_lhb_institutional",
        lambda code: {"vote": 1, "detail": "bullish", "raw": {}},
    )
    monkeypatch.setattr(
        inst, "_fetch_shareholder_count",
        lambda code: {"vote": 1, "detail": "bullish", "raw": {}},
    )
    monkeypatch.setattr(
        inst, "_fetch_top10_institutional_ratio", lambda code: None
    )

    result = inst.score_institutional_holding("920045", turnover_available=False)

    assert result["vote_score"] == 0
    assert result["vote_label"] == "机构数据不足(降权)"
    assert result["data_sufficient"] is False
    assert result["downweighted"] is True
    assert result["votes"]["main_force"]["raw"]["uncovered_market"] is True


def test_backoff_allows_one_probe_after_expiry(monkeypatch):
    inst._main_force_fallback_failures = 1
    inst._main_force_fallback_block_until = time.monotonic() - 1
    frame = pd.DataFrame(
        {"代码": ["600000"], "3日主力净流入-净额": [50000000.0]}
    )

    monkeypatch.setattr(inst, "call_ak_with_retry", lambda func, **kwargs: frame)

    result = inst._fetch_main_force_flow_fallback("600000")

    assert result is not None
    assert result["vote"] == 1
    assert inst._main_force_fallback_failures == 0
    assert inst._main_force_fallback_block_until == 0.0
