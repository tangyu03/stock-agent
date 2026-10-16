import sys
from datetime import date
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from test_f0_audit_fixes import _make_lifecycle
l, e = _make_lifecycle()
print(e)
notices=l.evaluate_events("002975", current_price=102.0, day_low=102.0, day_high=104.0, close_price=103.0, today=date(2026,9,9))
print(notices)
print("events", l.get_active_events("002975"), l.get_prior_events("002975"))
print("note", l.event_status_note("002975", current_price=103.0))
