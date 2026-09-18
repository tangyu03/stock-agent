lines = open('src/analyzers/timing_engine.py', encoding='utf-8').read().splitlines()
print('\n'.join(f'{i+1}: {l}' for i, l in enumerate(lines[2675:2690], start=2675)))
