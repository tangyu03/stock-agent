# -*- coding: utf-8 -*-
import io
p = r"C:\Users\15831\Downloads\stock-agent2\tests\test_data_source_fixes.py"
s = io.open(p, encoding="utf-8").read()
old = '        assert "\u975e\u6807\u51c6\u62a5\u544a\u671f" in result["detail"]\n        assert "\u62a5\u544a\u671f" not in result["detail"]\n'
new = '        assert "\u975e\u6807\u51c6\u62a5\u544a\u671f" in result["detail"]\n        assert not result["detail"].startswith("\u62a5\u544a\u671f")\n'
assert old in s, "target not found"
io.open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("assert fixed OK")
