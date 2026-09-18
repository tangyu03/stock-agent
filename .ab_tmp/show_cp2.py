lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[1246:1268], start=1246)))
print('===== _completion_price =====')
for i, l in enumerate(lines, 1):
    if 'def _completion_price' in l:
        print('\n'.join(f'{j+1}: {lines[j]}' for j in range(i-1, min(i+30, len(lines)))))
        break
