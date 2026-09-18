import os, re
hits = []
for root, dirs, files in os.walk('tests'):
    for f in files:
        if not f.endswith('.py'): continue
        p = os.path.join(root, f)
        s = open(p, encoding='utf8', errors='replace').read()
        for kw in ('_chunk_content', 'MAX_CONTENT_LEN', '<br/>', '<hr/>', 'chunk'):
            for m in re.finditer(re.escape(kw), s):
                hits.append((p, kw, m.start()))
for p, kw, i in hits[:40]:
    print(p, kw, i)
