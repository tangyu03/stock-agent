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

    def _send(self, title, content, level="常规"):
        captured.update({"title": title, "content": content})
        return True

    monkeypatch.setattr(PushPlus, "send", _send)
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
