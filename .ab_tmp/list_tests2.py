lines = open('tests/test_volume_pattern.py', encoding='utf-8').read().splitlines()
print(len(lines), "lines total")
for i, l in enumerate(lines, 1):
    if l.startswith('class Test') or l.startswith('    def test_'):
        print(i, l)
