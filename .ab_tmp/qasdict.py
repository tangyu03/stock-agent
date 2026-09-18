import re
s=open('src/analyzers/timing_engine.py',encoding='utf8').read()
for m in re.finditer(r'def as_dict', s):
    k=m.start()
    seg=s[max(0,k-300):k+900]
    print('=== @', k, '===')
    print(seg)
    print()
