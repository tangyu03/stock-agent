# -*- coding: utf-8 -*-

def patch(path, pairs, label):
    s = open(path, encoding="utf-8").read()
    for old, new in pairs:
        if old not in s:
            print("[MISS] " + label + ": " + old[:70])
            raise SystemExit(1)
    for old, new in pairs:
        s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[OK] " + label)

# 1. market_mode_adaptive.py：跌破MA5撤退不被派发日过期降级
patch(r"src/loop/market_mode_adaptive.py", [
    (
"""        if dist.get("stale"):
            mode_before = "defend"
            reasons.append(
                f"派发日数据过期(截至{dist.get('last_date', '?')}，"
                f"滞后{dist.get('stale_days', 0)}天)，模式降级防守"
            )""",
"""        if dist.get("stale"):
            if mode_before == "retreat":
                # 跌破MA5×0.99 是硬结构信号，独立于派发日新鲜度；
                # 不得把撤退降级成更不保守的防守（越破越激进的反向保护）。
                reasons.append(
                    f"派发日数据过期(截至{dist.get('last_date', '?')}，"
                    f"滞后{dist.get('stale_days', 0)}天)；已跌破MA5撤退为结构信号，不降级"
                )
            else:
                mode_before = "defend"
                reasons.append(
                    f"派发日数据过期(截至{dist.get('last_date', '?')}，"
                    f"滞后{dist.get('stale_days', 0)}天)，模式降级防守"
                )""",
    ),
], "mode stale retreat")

# 2. templates.py：防守档转进攻提示补全条件
patch(r"src/push/templates.py", [
    (
"""    if mode == "defend":
        return "今日若收盘站回MA5，明日转进攻\"""",
"""    if mode == "defend":
        return "今日若收盘站回MA5且多头排列(MA5>MA10>MA20)且派发日≤5，明日转进攻\"""",
    ),
], "mode gate expectation")

# 3. 更新断言
patch(r"tests/test_p220_audit_defects.py", [
    (
"""    assert _mode_gate_expectation("defend") == "今日若收盘站回MA5，明日转进攻\"""",
"""    assert _mode_gate_expectation("defend") == "今日若收盘站回MA5且多头排列(MA5>MA10>MA20)且派发日≤5，明日转进攻\"""",
    ),
], "test mode expectation")

print("DONE")
