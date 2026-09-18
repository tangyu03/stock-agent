import re
s=open('src/analyzers/timing_engine.py',encoding='utf8').read()
for m in re.finditer(r'class \w*[Vv]olume\w*', s):
    k=m.start()
    print('=== class @', k, '===')
    print(s[k:k+300])
    print()
for m in re.finditer(r'volume_ratio_sample_time', s):
    k=m.start()
    seg=s[max(0,k-120):k+200].replace('\n',' ')
    print('ref: ...', seg, '...')
