from datetime import date

from src.feedback.event_tracker import (
    build_virtual_fill_counts,
    build_watch_ladder,
    collect_in_flight_events,
    render_in_flight_events,
)


class TestInFlightBroadcast:
    """广播表只看信号事件，不读持仓、不推断仓位。"""

    def _event(self, code, name, status, born="2026-09-08", **kw):
        row = {
            "event_id": f"evt-{code}",
            "stock_code": code,
            "stock_name": name,
            "entry_type": "确认追强",
            "born_date": born,
            "expire_date": "2026-09-13",
            "entry_price": 544.14,
            "stop_loss": 498.26,
            "target_low": 583.01,
            "status": status,
            "invalid_reason": "",
        }
        row.update(kw)
        return row

    def test_collect_keeps_active_and_recent_closed_but_filters_future(self):
        events = [
            self._event("000002", "沃尔德", "valid"),
            self._event("000003", "博杰股份", "invalidated",
                        invalid_reason="回踩失败已撤单"),
            self._event("000004", "过期旧事件", "expired", born="2026-08-25"),
            self._event("000005", "未来事件", "valid", born="2026-09-09"),
        ]
        rows = collect_in_flight_events(
            as_of=date(2026, 9, 8), lookback_days=7, events=events)

        assert [r["stock_code"] for r in rows] == ["000002", "000003"]
        assert rows[0]["active"] is True
        assert rows[0]["status_label"] == "已触发"
        assert rows[1]["active"] is False
        assert rows[1]["status_label"] == "已撤单"

    def test_render_shows_risk_lines_and_state(self):
        rows = collect_in_flight_events(
            as_of=date(2026, 9, 8), events=[
                self._event("000001", "蘅东光通讯", "triggered",
                            current_price=549.63),
            ])
        text = render_in_flight_events(rows)

        assert "在飞事件 1（活跃1/完结0）" in text
        assert "蘅东光通讯 确认追强 已成交" in text
        assert "#evt-000001" not in text
        assert "#t-000001" in text
        assert "买544.14" in text
        assert "止损498.26" in text
        assert "目标583.01" in text
        assert 'RRR0.85' in text
        assert "现价549.63" in text
        assert "距买点+1.0%" in text
        assert "至2026-09-13" in text

    def test_render_empty_is_honest(self):
        assert "今日无合格事件" in render_in_flight_events([])

    def test_frozen_event_is_active_and_chase_abandon_is_closed(self):
        rows = collect_in_flight_events(
            as_of=date(2026, 9, 8), events=[
                self._event("000001", "冻结事件", "frozen"),
                self._event("000002", "追高放弃", "chase_abandon",
                            invalid_reason="价格远离买点，回踩假设失效"),
            ])

        assert rows[0]["active"] is True
        assert rows[0]["status_label"] == "已冻结"
        assert rows[1]["active"] is False
        assert rows[1]["status_label"] == "追高放弃"


class TestWatchLadderAndCounts:
    """候梯排序和虚拟撮合只做最小可审计信息。"""

    def test_watch_ladder_sorts_by_missing_condition_count(self):
        diagnostics = {
            "600001": "评分: 2/6\n策略检查:\n- ADX不足\n评分: 2/6",
            "600002": "评分: 1/6\n策略检查:\n- ADX不足\n- 外盘不足\n评分: 1/6",
            "600003": "评分: 0/6\n策略检查:\n- 差A\n- 差B\n- 差C\n评分: 0/6",
        }
        stocks = [
            {"code": "600002", "name": "两条件"},
            {"code": "600001", "name": "一条件"},
            {"code": "600003", "name": "三条件"},
        ]
        rows = build_watch_ladder(diagnostics, stocks)

        assert [r["stock_code"] for r in rows] == ["600001", "600002", "600003"]
        assert rows[0]["fail_count"] == 1
        assert rows[0]["reason"] == "综合(当前模式可评估)：缺 ADX不足"
        assert rows[2]["reason"] == "综合(当前模式可评估)：缺 差A+差B等3项"

    def test_watch_ladder_shows_closest_strategy_and_missing_conditions(self):
        diagnostics = {
            "688028": (
                "技术偏多但四种入场策略均未达到触发阈值\n"
                "策略检查:\n"
                "- 确认追强: 防守模式降仓放行需过三重门"
                "(四确认+基本面+板块联动)——未过: 门一未过: ADX单边力度\n"
            ),
            "300666": (
                "技术偏多但四种入场策略均未达到触发阈值\n"
                "策略检查:\n"
                "- 确认追强: 防守模式降仓放行需过三重门"
                "(四确认+基本面+板块联动)——未过: "
                "门一未过: ADX单边力度、外盘主动\n"
            ),
        }
        stocks = [
            {"code": "300666", "name": "深科达"},
            {"code": "688028", "name": "沃尔德"},
        ]
        rows = build_watch_ladder(diagnostics, stocks)

        assert [r["stock_code"] for r in rows] == ["688028", "300666"]
        assert rows[0]["reason"] == "确认追强(当前模式可评估)：缺 ADX"
        assert rows[1]["reason"] == "确认追强(当前模式可评估)：缺 ADX+外盘主动"

    def test_watch_ladder_excludes_active_in_flight_event(self):
        diagnostics = {
            "002975": (
                "技术偏多但四种入场策略均未达到触发阈值\n策略检查:\n"
                "- 恐慌抄底: 正常行情，未触发\n"
            ),
        }
        in_flight = [{
            "event_id": "evt-20260907112434-002975-价量突破",
            "stock_code": "002975",
            "entry_type": "价量突破",
            "status": "filled",
            "status_label": "已成交",
            "active": True,
        }]
        rows = build_watch_ladder(
            diagnostics,
            [{"code": "002975", "name": "博杰股份"}],
            in_flight_events=in_flight,
        )

        assert rows == []

    def test_watch_ladder_separates_cross_mode_candidates(self):
        diagnostics = {
            "002975": "策略检查:\n- 恐慌抄底: 正常行情，未触发\n",
            "688028": "策略检查:\n- 确认追强: ——未过: ADX单边力度\n",
        }
        stocks = [
            {"code": "002975", "name": "博杰股份"},
            {"code": "688028", "name": "沃尔德"},
        ]
        rows = build_watch_ladder(
            diagnostics, stocks, market_mode="defend",
        )

        assert rows[0]["stock_code"] == "688028"
        assert rows[0]["cross_mode"] is False
        assert "当前模式可评估" in rows[0]["reason"]
        assert rows[1]["stock_code"] == "002975"
        assert rows[1]["cross_mode"] is True
        assert rows[1]["mode_required"] == "恐慌/撤退"
        assert "需市场模式进入恐慌/撤退" in rows[1]["reason"]

    def test_sector_retreat_does_not_create_hard_block(self):
        rows = build_watch_ladder(
            {
                "301666": (
                    "板块:退潮\n策略检查:\n"
                    "- 确认追强: 缺 ADX\n"
                ),
            },
            [{"code": "301666", "name": "大普微"}],
            market_mode="defend",
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.get("hard_blocked") is not True
        assert "缺 ADX" in row["reason"]
        assert "当前模式可评估" in row["reason"]

    def test_virtual_fill_counts_uses_lifecycle_states(self):
        rows = [
            {"status": "triggered", "invalid_reason": ""},
            {"status": "valid", "invalid_reason": ""},
            {"status": "invalidated", "invalid_reason": "回踩失败已撤单"},
            {"status": "invalidated", "invalid_reason": "收盘跌破止损线"},
            {"status": "expired", "invalid_reason": ""},
            {"status": "chase_abandon", "invalid_reason": ""},
        ]
        text = build_virtual_fill_counts(rows, target=30)

        assert "成交1" in text
        assert "结构失败撤单2" in text
        assert "信号止损0" in text
        assert "到期1" in text
        assert "追高放弃1" in text
        assert "等待回踩1" in text
        assert "累计完结样本4/30" in text

    def test_structural_stop_is_not_signal_stop(self):
        from src.signal_states import canonical_match_status

        assert canonical_match_status(
            "invalidated", "收盘474.00跌破止损线476.75，结构失败，买单撤单"
        ) == "struct_cancel"
        assert canonical_match_status("invalidated", "先触及止损价") == "sig_stop"

    def test_unbacked_historical_count_is_disclosed(self):
        text = build_virtual_fill_counts(
            [], target=30, pending_historical={"sig_stop": 1},
        )
        assert "信号止损1" in text
        assert "历史事件，早于09-09，待补录" in text

    def test_cancel_word_only_applies_to_structural_cancel(self):
        rows = [
            {"status": "invalidated", "invalid_reason": "回踩失败已撤单"},
            {"status": "invalidated", "invalid_reason": "收盘跌破止损线"},
            {"status": "sig_stop", "invalid_reason": ""},
        ]
        text = build_virtual_fill_counts(rows, target=30)

        assert "结构失败撤单2" in text
        assert "信号止损1" in text
        assert " 撤单" not in text
