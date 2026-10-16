# -*- coding: utf-8 -*-
p = r"tests/test_p220_audit_defects.py"
s = open(p, encoding="utf-8").read()
old = '''        assert "闸门预期:今日若收盘站回MA5，明日转进攻" in html'''
new = '''        assert "闸门预期:今日若收盘站回MA5且多头排列(MA5>MA10>MA20)且派发日≤5，明日转进攻" in html'''
assert old in s
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("OK")
