lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
for i, l in enumerate(lines, 1):
    if '_completion_price' in l or 'completion_price' in l or 'completed_price' in l or 'close_price' in l or 'exit_price' in l:
        print(i, l)
