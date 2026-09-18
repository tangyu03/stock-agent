lines = open('tests/test_volume_pattern.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[0:40], start=1)))
