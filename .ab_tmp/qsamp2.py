import re, os
for fn in ['src/analyzers/timing_engine.py','src/orchestrator/engine.py','src/decision/live_scheduler.py']:
    try:
        s=open(fn,encoding='utf8').read()
    except FileNotFoundError:
        continue
    for m in re.finditer(r'volume_ratio_sample_time|sample_time', s):
        i=m.start()
        seg=s[max(0,i-120):i+220].replace('\n',' ')
        print(f'{fn}: ...{seg}...')
        print()
