lines = open('tests/test_f1_audit_fixes.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[1:10], start=1)))
print('...TAIL...')
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[104:121], start=104)))
