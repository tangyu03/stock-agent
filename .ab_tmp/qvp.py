import re
s=open('src/analyzers/volume_pattern.py',encoding='utf8').read()
print('confirms occurrences:', len(re.findall(r'"confirms"\s*:', s)))
for m in re.finditer(r'return\s*\{', s):
    i=m.start()
    seg=s[i:i+1500]
    if 'summary_short' in seg or 'pattern' in seg:
        print('=== return dict @', i, '===')
        print(seg[:1500])
        break
else:
    print('no return dict found with those keys; listing function bounds')
    i=s.find('def build_volume_pattern')
    j=s.find('\ndef ', i+10)
    body=s[i:j]
    print('function length', len(body))
    for m in re.finditer(r'"pattern"|"confirms"|"confirm_target"|"summary_short"|"position"', body):
        k=m.start()
        print('...', body[max(0,k-60):k+80].replace('\n',' '))
