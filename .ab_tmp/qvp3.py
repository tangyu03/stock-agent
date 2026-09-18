import re
s=open('src/analyzers/volume_pattern.py',encoding='utf8').read()
i=s.find('def build_volume_pattern')
j=s.find('def ', i+10)
body=s[i:j]
for m in re.finditer(r'drive', body):
    k=m.start()
    line=body[max(0,k-80):k+120].replace('\n',' ')
    print('...', line)
