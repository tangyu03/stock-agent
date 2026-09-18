lines = open('tests/test_p0_audit_0916.py', encoding='utf-8').read().splitlines()
print(len(lines), "lines total")
for i, l in enumerate(lines, 1):
    if l.startswith('def test_'):
        print(i, l)
