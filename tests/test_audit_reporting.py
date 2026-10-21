import pytest

from src.loop.market_mode_adaptive import MarketModeAdaptive


def _index(bullish=True):
    base = 100.0
    kline = []
    for i in range(30):
        close = base + (i if bullish else -i * 0.35)
        kline.append({
            "date": f"2026-09-{i + 1:02d}",
            "open": close - 0.1,
            "close": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "volume": 1_000_000 + (10_000 if bullish else -10_000) * i,
        })
    return kline


def test_environment_score_detail_is_reproducible(monkeypatch):
    monkeypatch.setattr(MarketModeAdaptive, "_get_advance_decline", lambda self: {
        "advance": 1200, "decline": 800, "flat": 100, "ratio": 1.5, "available": True,
    })
    result = MarketModeAdaptive().score_dimensions("2026-09-30", _index())
    assert "评分明细:" in result["score_detail"]
    assert "指数趋势" in result["score_detail"] and "×0.25" in result["score_detail"]
    weighted = sum(d["weighted_value"] for d in result["dimensions"] if "weight" in d)
    assert result["raw_score"] == pytest.approx(5 + weighted * 5, abs=0.05)



def test_freshness_annotation_marks_stale_source():
    from src.push.templates import _institutional

    data = {
        "institutional_holding": {
            "vote_score": 0,
            "vote_label": "资金中性",
            "bullish_count": 0,
            "bearish_count": 1,
            "valid_vote_sources": 0,
            "vote_weights": {"shareholder": 0.5},
            "vote_freshness": {
                "shareholder": {"date": "2026-06-30", "age_days": 72, "display_only": True},
            },
            "votes": {
                "shareholder": {"vote": -1, "detail": "股东户数增加", "raw": {}},
            },
        }
    }
    text = _institutional(data)
    assert "@06/30" in text
    assert "(滞后,仅展示)" in text
    assert "有效票源0/4" in text

def test_stale_vote_weight_is_zero_and_t_minus_one_full():
    from src.analyzers.institutional_scorer import _effective_vote_weight

    assert _effective_vote_weight("north_bound", 1) == 1.0
    assert _effective_vote_weight("north_bound", 4) == 0.5
    assert _effective_vote_weight("lhb", 6) == 0.0
    assert _effective_vote_weight("main_force", 2) == 0.5
    assert _effective_vote_weight("main_force", None) == 0.5