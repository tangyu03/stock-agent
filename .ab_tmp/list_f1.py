lines = open('tests/test_f1_audit_fixes.py', encoding='utf-8').read().splitlines()
print(len(lines), "lines")
for i, l in enumerate(lines, 1):
    if l.startswith('def test_'):
        print(i, l)
