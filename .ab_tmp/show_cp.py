lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[1285:1330], start=1285)))
