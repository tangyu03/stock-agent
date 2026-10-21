# -*- coding: utf-8 -*-
"""P2-18 工程呈现：分页原子化 + 报告头数据时点。

验收标准：
  - 分页以个股为原子：<hr/> 卡片边界切分，单股卡片不跨块
  - 报告头加数据时点：MM-DD HH:MM｜交易日进度 N%（14:07 → 78%）
"""


def test_chunk_content_keeps_cards_atomic():
    from src.push.pushplus import PushPlus

    cards = [f"<b>卡{i} 名称{i}(00{i})</b><br/>" + "x" * 3000 + "<br/>" for i in range(6)]
    content = "<hr/>".join(cards)
    chunks = PushPlus._chunk_content(content, max_len=10000)
    assert len(chunks) > 1
    for chunk in chunks:
        for i in range(6):
            if f"卡{i}" in chunk:
                assert f"名称{i}" in chunk, f"chunk 内 卡{i} 被切断"


def test_chunk_content_short_content_single_chunk():
    from src.push.pushplus import PushPlus

    content = "<b>小内容</b><br/>"
    assert PushPlus._chunk_content(content, max_len=10000) == [content]


def test_latest_sample_time_picks_max():
    from src.push.pushplus import PushPlus

    signals = [
        {"tech_signals": {"volume_snapshot": {"volume_ratio_sample_time": "20260917090500"}}},
        {"tech_signals": {"volume_snapshot": {"same_period_sample_time": "20260917140200"}}},
        {"tech_signals": {"volume_snapshot": {}}},
    ]
    assert PushPlus._latest_sample_time(signals) == "20260917140200"
    assert PushPlus._latest_sample_time([]) == ""
    assert PushPlus._latest_sample_time([{}]) == ""


def test_trading_progress_matches_acceptance():
    from src.push.pushplus import PushPlus

    # 14:07 → 187/240 ≈ 78%（验收样例）
    assert PushPlus._trading_progress("20260917140700") == 78
    assert PushPlus._trading_progress("20260917093000") == 0
    assert PushPlus._trading_progress("20260917113000") == 50
    assert PushPlus._trading_progress("20260917130000") == 50
    assert PushPlus._trading_progress("20260917150000") == 100
    assert PushPlus._trading_progress("bad") == 0


def test_send_intraday_report_prepends_data_time(monkeypatch):
    from src.push.pushplus import PushPlus

    captured = {}

    def _send(self, title, content, level="常规", **kwargs):
        captured.update({"title": title, "content": content, **kwargs})
        return True

    monkeypatch.setattr(PushPlus, "send", _send)
    monkeypatch.setattr(PushPlus, "_load_report_snapshot", staticmethod(lambda: None))
    monkeypatch.setattr(
        PushPlus, "_save_report_snapshot", staticmethod(lambda snapshot: None)
    )
    PushPlus.__new__(PushPlus).send_intraday_report(
        {"market_mode": "defend", "market_score": 5.0},
        entries=[{
            "stock_name": "中科飞测",
            "stock_code": "688361",
            "entry_type": "价量突破",
            "current_price": 90.2,
            "tech_signals": {"volume_snapshot": {"volume_ratio_sample_time": "20260917140700"}},
        }],
    )
    assert "数据时点 09-17 14:07｜交易日进度 78%" in captured["content"]
    # 行动摘要仍在页首
    assert "今日行动摘要" in captured["content"]
    assert captured["page_header"] == "防守 5.0｜09-17 14:07｜交易日进度 78%"


def test_send_intraday_report_uses_four_layer_order(monkeypatch):
    from src.push.pushplus import PushPlus
    import src.feedback.event_tracker as event_tracker

    captured = {}

    def _send(self, title, content, level="常规", **kwargs):
        captured.update({"title": title, "content": content})
        return True

    monkeypatch.setattr(PushPlus, "send", _send)
    monkeypatch.setattr(event_tracker, "collect_in_flight_events", lambda **kwargs: [])
    monkeypatch.setattr(PushPlus, "_load_report_snapshot", staticmethod(lambda: None))
    monkeypatch.setattr(
        PushPlus, "_save_report_snapshot", staticmethod(lambda snapshot: None)
    )
    PushPlus.__new__(PushPlus).send_intraday_report(
        {
            "market_mode": "defend", "market_score": 5.0,
            "score_gate": {
                "count": 1,
                "detail": "天孚通信(300394) | 评分闸门: 综合评分1/6≤1",
            },
        },
        entries=[{
            "stock_name": "天孚通信", "stock_code": "300394",
            "entry_type": "恐慌抄底", "current_price": 100.0,
            "event_id": "evt-300394",
        }],
    )

    content = captured["content"]
    positions = [content.index(label) for label in (
        "① 结论与今日行动", "② 现役事件", "③ 相对上一推送 DIFF", "④ 附录：环境与信号全文",
    )]
    assert positions == sorted(positions)
    assert content.index("#t-300394") < content.index("③ 相对上一推送 DIFF")
    assert "综合评分1/6≤1" in content
    assert "口径: 技术同向｜机构同向｜主线板块｜无冲突｜环境≥6" in content


def test_gate_items_preserve_score_and_evidence(monkeypatch):
    from src.push.pushplus import PushPlus
    import src.feedback.event_tracker as event_tracker

    captured = {}
    monkeypatch.setattr(PushPlus, "send", _capture_send(captured))
    monkeypatch.setattr(event_tracker, "collect_in_flight_events", lambda **kwargs: [])
    monkeypatch.setattr(PushPlus, "_load_report_snapshot", staticmethod(lambda: None))
    monkeypatch.setattr(
        PushPlus, "_save_report_snapshot", staticmethod(lambda snapshot: None)
    )
    PushPlus.__new__(PushPlus).send_intraday_report(
        {
            "market_mode": "defend", "market_score": 5.0,
            "score_gate": {
                "count": 1,
                "items": [{
                    "stock_name": "创世纪", "stock_code": "300083",
                    "reason": "评分提示: 置信度评分0/6，RRR0.37，仅显著披露",
                    "score": 0,
                    "applicable_score": 6,
                    "min_confidence_score": 2,
                    "confidence_details": ["技术票+0.5", "机构票0"],
                }],
            },
        },
    )

    content = captured["content"]
    assert "创世纪(300083)｜评分0/6｜RRR0.37｜依据:技术票+0.5；机构票0" in content


def test_send_intraday_report_diffs_against_last_snapshot(monkeypatch):
    from src.push.pushplus import PushPlus
    import src.feedback.event_tracker as event_tracker

    captured = {}
    saved = []
    previous = {
        "market_mode": "attack",
        "market_score": 6.0,
        "stocks": {
            "688008": {
                "identity": "澜起科技(688008)",
                "role": "entry",
                "event_id": "evt-old",
                "entry_type": "恐慌抄底",
                "status": "valid",
                "status_label": "挂单中",
                "active": True,
            },
            "301666": {
                "identity": "大普微(301666)",
                "role": "entry",
                "event_id": "evt-dapu",
                "entry_type": "确认追强",
                "status": "filled",
                "status_label": "已触发",
                "active": True,
            },
        },
            "gate_codes": ["603061"],
            "gate_detail": "金海通(603061) | 评分提示",
        "no_add_codes": [],
        "missing_codes": [],
        "sector_concentration": "",
    }
    monkeypatch.setattr(PushPlus, "send", _capture_send(captured))
    monkeypatch.setattr(event_tracker, "collect_in_flight_events", lambda **kwargs: [])
    monkeypatch.setattr(PushPlus, "_load_report_snapshot", staticmethod(lambda: previous))
    monkeypatch.setattr(
        PushPlus,
        "_save_report_snapshot",
        staticmethod(lambda snapshot: saved.append(snapshot)),
    )
    PushPlus.__new__(PushPlus).send_intraday_report(
        {
            "market_mode": "defend",
            "market_score": 5.0,
            "score_gate": {"count": 1, "detail": "天孚通信(300394) | 评分闸门"},
            "data_missing": [{
                "stock_name": "天孚通信", "stock_code": "300394", "reason": "现价未取到",
            }],
        },
        entries=[{
            "stock_name": "芯原股份", "stock_code": "688008",
            "entry_type": "回踩MA5", "event_id": "evt-new",
        }],
    )

    diff = captured["content"][captured["content"].index("③ 相对上一推送 DIFF"):
        captured["content"].index("④ 附录：环境与信号全文")]
    assert "模式变化: 进攻 → 防守" in diff
    assert "新触发: 芯原股份(688008) 回踩MA5" in diff
    assert "状态变化: 澜起科技(688008) 挂单中 → 无" not in diff
    assert "买入提示新增: 天孚通信(300394)" in diff
    assert "买入提示解除: 金海通(603061)" in diff
    assert "数据缺失新增: 300394" in diff
    assert "退出: 大普微(301666)" in diff
    assert saved and saved[0]["stocks"]["688008"]["event_id"] == "evt-new"


def test_failed_send_does_not_replace_last_snapshot(monkeypatch):
    from src.push.pushplus import PushPlus

    saved = []
    monkeypatch.setattr(PushPlus, "send", lambda self, title, content, level="常规", **kwargs: False)
    monkeypatch.setattr(PushPlus, "_load_report_snapshot", staticmethod(lambda: None))
    monkeypatch.setattr(
        PushPlus,
        "_save_report_snapshot",
        staticmethod(lambda snapshot: saved.append(snapshot)),
    )
    PushPlus.__new__(PushPlus).send_intraday_report({}, entries=[{
        "stock_name": "芯原股份", "stock_code": "688008",
    }])

    assert saved == []


def _capture_send(captured):
    def _send(self, title, content, level="常规", **kwargs):
        captured.update({"title": title, "content": content, "level": level})
        return True
    return _send
