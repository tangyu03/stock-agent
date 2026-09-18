# -*- coding: utf-8 -*-
"""P2-17 今日行动摘要：页首一行一人（触发事件｜待验证条件｜需盯价格），
首屏即可回答"今天动不动"（27 票终点原为"防守"二字）。
"""


def _entry():
    return {
        "stock_name": "中科飞测", "stock_code": "688361", "entry_type": "价量突破",
        "trigger_price": 88.5, "current_price": 90.2,
        "tech_signals": {"order_flow": {"available": True, "imbalance_pct": 2.0}, "rsi6": 60},
        "volume_ratio": 1.4, "turnover_rate": 5.0,
        "ma5": 87.0, "ma10": 85.0, "ma20": 83.0,
        "execution_plan": {"execution_tiers": [
            {"role": "probe", "price": 88.5, "name": "试探仓"},
            {"role": "main", "price": 89.8, "name": "主仓"},
        ]},
    }


def _exit():
    return {
        "stock_name": "金海通", "stock_code": "603061", "exit_type": "破位止损",
        "stop_loss_price": 72.1, "trigger_price": 72.1, "current_price": 71.9,
        "reason": "跌破止损", "urgency": "紧急",
    }


def _obs():
    return {
        "stock_name": "蘅东光", "stock_code": "873339", "current_price": 15.3,
        "change_pct": -1.2,
        "tech_signals": {"order_flow": {"available": True, "imbalance_pct": -3.0}, "rsi6": 55},
        "volume_ratio": 0.9, "turnover_rate": 2.0,
        "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
        "note": "缩量阴跌观察",
    }


def test_action_summary_renders_one_line_per_person():
    from src.push.templates import render_action_summary

    out = render_action_summary([_entry()], [_exit()], [_obs()])
    assert "今日行动摘要" in out
    assert "中科飞测(688361)" in out
    assert "金海通(603061)" in out
    assert "蘅东光(873339)" in out
    # 一人一行：三只票三行
    assert out.count("&nbsp;&nbsp;") == 3


def test_action_summary_has_trigger_confirm_watch():
    from src.push.templates import render_action_summary

    out = render_action_summary([_entry()], [_exit()], [_obs()])
    # 触发事件：买入/卖出
    assert "买入 价量突破" in out
    assert "卖出 破位止损" in out
    # 需盯价格：执行档/触发价
    assert "盯:" in out
    # 待验证条件：分型确认条件（观察票 confirms）
    assert "待验:" in out


def test_action_summary_empty_returns_empty():
    from src.push.templates import render_action_summary

    assert render_action_summary([], [], []) == ""
    assert render_action_summary(None, None, None) == ""


def test_action_summary_missing_price_degrades_gracefully():
    from src.push.templates import render_action_summary

    data = {
        "stock_name": "无名", "stock_code": "000000", "entry_type": "价量突破",
        "current_price": None, "execution_plan": {},
    }
    out = render_action_summary([data], [], [])
    assert "无名(000000)" in out
    assert "盯:" not in out
