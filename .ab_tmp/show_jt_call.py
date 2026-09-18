lines = open('src/analyzers/volume_pattern.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[804:816], start=804)))
