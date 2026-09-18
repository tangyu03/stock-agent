import re
s=open('src/push/templates.py',encoding='utf8').read()
i=s.find('def render_environment_overview')
j=s.find('\ndef ', i+10)
body=s[i:j]
print('render_environment_overview length', len(body))
print('<hr/> count:', body.count('<hr/>'))
print('<br/> count:', body.count('<br/>'))
# check sell card separator in pushplus
p=open('src/push/pushplus.py',encoding='utf8').read()
k=p.find('if exits:')
print('=== exits render block ===')
print(p[k:k+400])
