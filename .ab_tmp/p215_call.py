import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
path = 'src/analyzers/volume_pattern.py'
src = open(path, encoding='utf-8').read()
old = 'result["judgment"] = _judgment_text(data.get("change_pct"), vgear, drive, rsi6)'
new = 'result["judgment"] = _judgment_text(data.get("change_pct"), vgear, drive, rsi6, data.get("ma5"), close)'
assert src.count(old) == 1, src.count(old)
open(path, 'w', encoding='utf-8', newline='').write(src.replace(old, new))
print('patched call site')
