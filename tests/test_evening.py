from pointtable.jobs.evening import evening_report


def test_evening_report_wires_receipt_coverage():
    text = evening_report(
        '10.21',
        {'amount_yi': 8900, 'tier': 'LOW', 'index_level': 3731, 'zero_plan_day': True},
        [{'name': 'A', 'receipt_coverage': 0.6, 'missed_stop_date': '10.14'}],
        0.6,
    )
    assert '【10.21 复盘】' in text
    assert '明日零预案' in text
    assert '市场永远是对的' in text
