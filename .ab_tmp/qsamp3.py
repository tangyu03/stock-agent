import re
s=open('src/analyzers/timing_engine.py',encoding='utf8').read()
i=s.find('volume_ratio_sample_time')
# find the snapshot construction
for m in re.finditer(r'volume_ratio_sample_time', s):
    k=m.start()
    seg=s[max(0,k-300):k+300].replace('\n',' ')
    print('...', seg)
    print()
