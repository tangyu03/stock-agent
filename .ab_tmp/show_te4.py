lines = open('src/analyzers/timing_engine.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[2638:2674], start=2638)))
