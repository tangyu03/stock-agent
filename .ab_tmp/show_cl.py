lines = open('CHANGELOG.md', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[40:60], start=40)))
