from src.decision.live_scheduler import format_scheduled_summary, schedule_live_signals


def test_multiple_signals_are_not_capped_by_count():
    signals = [
        {
            "stock_code": f"{600000 + i:06d}",
            "stock_name": f"股票{i}",
            "entry_type": "价量突破",
            "trigger_price": 10.0,
            "confidence": "中",
        }
        for i in range(6)
    ]
    scheduled = schedule_live_signals(signals, [], total_asset=2_000_000)
    summary = format_scheduled_summary(scheduled)

    assert scheduled["stats"]["buy_executed"] == 6
    assert scheduled["stats"]["buy_skipped_no_budget"] == 0
    assert "单次建议上限" not in summary


def test_buy_signals_ignore_total_asset_budget():
    signals = [
        {
            "stock_code": "688001",
            "stock_name": "高价股1",
            "entry_type": "价量突破",
            "trigger_price": 500.0,
        },
        {
            "stock_code": "688002",
            "stock_name": "高价股2",
            "entry_type": "价量突破",
            "trigger_price": 800.0,
        },
    ]

    scheduled = schedule_live_signals(signals, [], total_asset=10_000)

    assert scheduled["stats"]["buy_executed"] == 2
    assert scheduled["stats"]["buy_skipped_no_budget"] == 0
    assert scheduled["stats"]["buy_skipped_dust_order"] == 0
    # 【一】【六】新增两类跳过桶（假说拒绝/策略下线），默认为空
    assert scheduled["skipped"] == {
        "buy_no_budget": [],
        "buy_dust_order": [],
        "buy_low_confidence": [],
        "buy_hypothesis_rejected": [],
        "buy_strategy_offline": [],
    }
    assert scheduled["stats"]["buy_hypothesis_rejected"] == 0
    assert scheduled["stats"]["buy_strategy_offline"] == 0
    assert "跳过信号" not in format_scheduled_summary(scheduled)
