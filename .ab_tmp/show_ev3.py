lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[840:882], start=840)))
