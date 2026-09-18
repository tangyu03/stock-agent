import re
s=open('src/analyzers/volume_pattern.py',encoding='utf8').read()
i=s.find('def active_drive')
print(s[i:i+800] if i>=0 else 'not found')
i=s.find('def classify')
print('=== classify head ===')
print(s[i:i+700])
