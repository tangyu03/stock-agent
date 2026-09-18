lines = open('tests/test_phase5_rework.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[350:395], start=350)))
