lines = open('src/analyzers/volume_pattern.py', encoding='utf-8').read().splitlines()
for i, l in enumerate(lines, 1):
    if '早盘观察' in l or 'defs = _PATTERN_DEFS' in l or '"star": 1 if pattern' in l or 'pattern is None' in l:
        print(i, l)
