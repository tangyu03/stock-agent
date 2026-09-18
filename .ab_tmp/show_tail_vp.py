lines = open('tests/test_volume_pattern.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[525:555], start=525)))
