lines = open('src/analyzers/signal_lifecycle.py', encoding='utf-8').read().splitlines()
for i, l in enumerate(lines, 1):
    if '"price": effective_price' in l or '"close": effective_price' in l or '"close": close or current_price' in l or '"price": effective_current' in l or '"close": close, "stop_loss"' in l or 'mark_chase_abandon(' in l or 'mark_signal_stop(' in l or 'mark_signal_target(' in l or 'mark_time_exit(' in l or 'self.expire(' in l or 'self.invalidate(' in l:
        print(i, l)
