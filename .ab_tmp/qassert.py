import os, re
for root, dirs, files in os.walk('tests'):
    for f in files:
        if not f.endswith('.py'): continue
        p = os.path.join(root, f)
        s = open(p, encoding='utf8', errors='replace').read()
        for m in re.finditer(r'content\[\:?\d*\]|\.startswith\(|assert .*content.*==', s):
            i = m.start()
            line = s[max(0,i-100):i+150].replace('\n',' ')
            print(f'{p}: ...{line}...')
            print()
