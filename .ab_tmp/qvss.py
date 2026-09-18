import re
s=open('src/analyzers/timing_engine.py',encoding='utf8').read()
for m in re.finditer(r'"volume_snapshot"', s):
    k=m.start()
    seg=s[max(0,k-250):k+250].replace('\n',' ')
    print('...', seg)
    print()
