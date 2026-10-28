from pointtable.notify.render import (
    render_closing,
    render_header,
    render_plan,
    render_position,
    render_report,
)


def test_header_slots_skip_when_missing():
    text = render_header({'amount_yi': 8900, 'tier': 'LOW'})
    assert '8900亿' in text
    assert 'MA5' not in text


def test_position_slots_and_terminal_sentiment():
    row = {
        'name': 'A', 'progress_pct': 20, 'up_day': True,
        'volume_ratio': 1.5, 'active_delta': 8, 'stop_low': 9.0,
        'stop_high': 9.2, 'trend': 'UP',
    }
    text = render_position(row)
    assert '信号生效至今 +20%。' in text
    assert '带量的阳线，外盘占优。' in text
    assert '止损9–9.2。' in text
    assert '拿来格局' in text


def test_missed_stop_is_shown_without_receipt_gate():
    row = {'name': 'A', 'missed_stop_date': '1.2'}
    assert '侥幸交税' in render_position(row)


def test_plan_and_report_shape():
    market = {'amount_yi': 8900, 'tier': 'LOW', 'index_level': 3731, 't_symbols': ['A']}
    text = render_report('10.21', market, [{'name': 'A'}])
    assert '【10.21 复盘】' in text
    assert '开盘先看3731' in text
    assert '少动就是赢' in text
