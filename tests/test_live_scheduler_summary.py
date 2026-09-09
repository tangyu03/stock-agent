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
        "buy_score_gate": [],
        "buy_ev_gate": [],
        "buy_strategy_offline": [],
    }
    assert scheduled["stats"]["buy_hypothesis_rejected"] == 0
    assert scheduled["stats"]["buy_strategy_offline"] == 0
    assert scheduled["stats"]["buy_ev_gate"] == 0
    assert "跳过信号" not in format_scheduled_summary(scheduled)


def test_invalid_zero_price_sell_is_not_scheduled():
    invalid = {
        "stock_code": "301666", "stock_name": "大普微",
        "exit_type": "破位止损", "trigger_price": 0, "stop_loss_price": 0,
        "reason": "跌破0.00(止损0.00)",
    }
    valid = {
        "stock_code": "688797", "stock_name": "臻宝",
        "exit_type": "技术走弱", "trigger_price": 256.99,
        "stop_loss_price": 239.21, "reason": "MACD死叉延续",
    }
    scheduled = schedule_live_signals([], [invalid, valid])

    assert scheduled["stats"]["sell_in"] == 2
    assert scheduled["stats"]["sell_executed"] == 1
    assert scheduled["stats"]["sell_skipped_invalid_price"] == 1
    assert [s.stock_code for s in scheduled["sell"]] == ["688797"]
    assert "无效价位 1" in format_scheduled_summary(scheduled)


def test_sell_with_valid_stop_but_missing_trigger_is_not_dropped():
    signal = {
        "stock_code": "688797", "stock_name": "臻宝",
        "exit_type": "破位止损", "trigger_price": 0,
        "stop_loss_price": 239.21, "reason": "有效止损锚",
    }
    scheduled = schedule_live_signals([], [signal])

    assert scheduled["stats"]["sell_executed"] == 1
    assert scheduled["stats"]["sell_skipped_invalid_price"] == 0
    assert scheduled["sell"][0].trigger_price == 0
    assert scheduled["sell"][0].stop_loss_price == 239.21
