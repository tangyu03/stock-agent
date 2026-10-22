import pandas as pd

import src.analyzers.institutional_scorer as inst
import src.data_layer.iwencai_api as iw
from src.push.templates import _institutional, _tech


def test_query_stock_fund_flow_parses_dated_series(monkeypatch):
    monkeypatch.setattr(
        iw,
        "_call_api",
        lambda **kwargs: {
            "datas": [
                {
                    "主力资金流向[20260901]": 1_170_000_000,
                    "主力资金流向[20260829]": -28_530_000,
                    "超大单净流入[20260901]": 1_170_000_000,
                    "大单净流入[20260901]": -200_000_000,
                    "换手率": 16.26,
                }
            ]
        },
    )

    result = iw.query_stock_fund_flow("301392")

    assert result["main_net"] == 1_170_000_000
    assert result["net_flows_5d"] == [-28_530_000, 1_170_000_000]
    assert result["net_flows_3d"] == [-28_530_000, 1_170_000_000]
    assert result["fund_flow_points_5d"] == [
        {"date": "20260829", "value": -28_530_000},
        {"date": "20260901", "value": 1_170_000_000},
    ]
    assert result["super_large_net"] == 1_170_000_000
    assert result["large_net"] == -200_000_000
    assert result["turnover_rate"] == 16.26


def test_main_force_flow_keeps_five_day_series_and_order_breakdown(monkeypatch):
    fund = {
        "main_net": 300.0,
        "net_flows_5d": [100.0, 200.0, 300.0, 400.0, 500.0],
        "super_large_flows_5d": [700.0, 800.0, 900.0, 1000.0],
        "large_flows_5d": [-70.0, -80.0, -90.0, -100.0],
    }
    monkeypatch.setattr(iw, "query_stock_fund_flow", lambda *args, **kwargs: fund)
    monkeypatch.setattr(
        inst,
        "_fetch_detailed_fund_flow",
        lambda code: (_ for _ in ()).throw(AssertionError("detail should not be called")),
    )

    result = inst._fetch_main_force_flow("301392")

    assert result["vote"] == 1
    assert result["raw"]["net_flows_5d"] == fund["net_flows_5d"]
    assert result["raw"]["latest_super_large_net"] == 1000.0
    assert result["raw"]["latest_large_net"] == -100.0


def test_main_force_vote_uses_five_day_total_without_extra_strong_vote():
    result = inst._evaluate_main_force_flows(
        [-100.0, -200.0, 300.0, -50.0, 1_000.0]
    )

    assert result["vote"] == 1
    assert result["raw"]["total"] == 950.0
    assert result["raw"]["strong"] is False
    assert result["raw"]["inflow_days"] == 2
    assert result["raw"]["outflow_days"] == 3


def test_detailed_fund_flow_parses_daily_rows(monkeypatch):
    frame = pd.DataFrame(
        {
            "日期": ["2026-08-29", "2026-09-01"],
            "主力净流入-净额": [-28_530_000, 1_170_000_000],
            "超大单净流入-净额": [-30_000_000, 1_050_000_000],
            "大单净流入-净额": [1_470_000, 120_000_000],
            "中单净流入-净额": [1_000_000, -50_000_000],
            "小单净流入-净额": [500_000, -80_000_000],
        }
    )
    monkeypatch.setattr(inst, "call_ak_with_retry", lambda func, **kwargs: frame)

    result = inst._fetch_detailed_fund_flow("301392")

    assert result["source"] == "stock_individual_fund_flow"
    assert result["main_flows_5d"][-1] == 1_170_000_000
    assert result["super_large_flows_5d"][-1] == 1_050_000_000
    assert result["large_flows_5d"][-1] == 120_000_000


def test_top10_institutional_ratio_is_summed_by_report_date(monkeypatch):
    monkeypatch.setattr(
        inst,
        "_latest_report_dates",
        lambda: ("20260630", "20260331"),
    )

    def fake_retry(func, **kwargs):
        if kwargs["date"] == "20260630":
            return pd.DataFrame(
                {
                    "股东性质": ["证券投资基金", "个人"],
                    "占总流通股本持股比例": [8.0, 3.0],
                }
            )
        return pd.DataFrame(
            {
                "股东性质": ["证券投资基金", "社保基金"],
                "占总流通股本持股比例": [6.0, 5.0],
            }
        )

    monkeypatch.setattr(inst, "call_ak_with_retry", fake_retry)

    result = inst._fetch_top10_institutional_ratio("301392")

    assert result["latest"]["ratio"] == 8.0
    assert result["previous"]["ratio"] == 11.0
    assert result["change_points"] == -3.0


def test_push_shows_turnover_rate():
    assert "换手:16.26%" in _tech({"turnover_rate": 16.26})


def test_push_shows_daily_fund_flow_sequence():
    text = _institutional(
        {
            "institutional_holding": {
                "vote_score": 0,
                "vote_label": "机构中性",
                "votes": {
                    "main_force": {
                        "vote": 0,
                        "detail": "主力资金流向不一",
                        "raw": {
                            "fund_flow_5d": [
                                {"date": "20260829", "value": -28_530_000},
                                {"date": "20260901", "value": 1_170_000_000},
                            ]
                        },
                    }
                },
            }
        }
    )

    assert "5日08/29流出2853万,09/01流入11.70亿" in text
