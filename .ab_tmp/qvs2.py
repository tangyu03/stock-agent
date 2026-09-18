import re
s=open('src/push/templates.py',encoding='utf8').read()
for m in re.finditer(r'volume_snapshot\s*=\s*', s):
    k=m.start()
    seg=s[max(0,k-200):k+300].replace('\n',' ')
    print('...', seg)
    print()
for m in re.finditer(r'get\("volume_ratio_sample_time"\)|get\("same_period_sample_time"\)', s):
    k=m.start()
    print('=== read @', k, '===')
    print(s[max(0,k-400):k+200])
    print()
