import re
s=open('src/analyzers/timing_engine.py',encoding='utf8').read()
for m in re.finditer(r'volume_ratio_sample_time|same_period_sample_time|volume_snapshot', s):
    k=m.start()
    line = s[max(0,k-160):k+200].replace('\n',' ')
    print(f'@{k}: ...{line}...')
    print()
