s=open('src/push/pushplus.py',encoding='utf8').read()
import re
for m in re.finditer(r'if exits:', s):
    i=m.start()
    seg=s[i:i+700]
    if '卖出信号' in seg or 'render_exit_signal' in seg:
        print('=== @', i, '===')
        print(seg)
        print()
