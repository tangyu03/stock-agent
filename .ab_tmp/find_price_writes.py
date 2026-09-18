lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
for i, l in enumerate(lines, 1):
    if '"price"' in l or '"close"' in l or '"day_low": low' in l:
        print(i, l)
