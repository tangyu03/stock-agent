lines = open('tests/test_p0_audit_0916.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[0:20], start=1)))
print('...TAIL...')
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[1180:1192], start=1180)))
