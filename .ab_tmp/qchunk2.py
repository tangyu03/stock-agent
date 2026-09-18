import os, re
hits = []
for root, dirs, files in os.walk('tests'):
    for f in files:
        if not f.endswith('.py'): continue
        p = os.path.join(root, f)
        s = open(p, encoding='utf8', errors='replace').read()
        for kw in ('<br/>', '<hr/>', 'chunk_content', 'send_intraday', '观察 (', '卖出信号', '买入信号'):
            idx = 0
            while True:
                i = s.find(kw, idx)
                if i < 0: break
                hits.append((p, kw, i))
                idx = i + 1
seen = set()
for p, kw, i in hits:
    if (p, kw) in seen: continue
    seen.add((p, kw))
    print(p, kw, i)
