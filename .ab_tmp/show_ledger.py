lines = open('src/feedback/signal_ledger.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[20:175], start=20)))
