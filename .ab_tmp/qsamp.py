import re, os
for fn in ['src/push/pushplus.py','src/push/templates.py','src/analyzers/volume_pattern.py']:
    s=open(fn,encoding='utf8').read()
    for m in re.finditer(r'sample_time|is_early_window|volume_ratio_sample', s):
        i=m.start()
        line = s[max(0,i-100):i+150].replace('\n',' ')
        print(f'{fn}: ...{line}...')
        print()
