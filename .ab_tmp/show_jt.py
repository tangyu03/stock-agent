lines = open('src/analyzers/volume_pattern.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[615:665], start=615)))
