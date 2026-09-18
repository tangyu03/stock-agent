import re
for path in ['tests/test_volume_pattern.py', 'tests/test_p0_audit_0916.py', 'tests/test_f1_audit_fixes.py', 'tests/test_event_arbitration.py', 'tests/test_audit_followups.py', 'tests/test_graded_exit_or.py']:
    try:
        text = open(path, encoding='utf-8').read()
    except Exception:
        continue
    for m in re.finditer(r'.*judgment.*|.*判读.*|.*完结价.*|.*已失效.*|.*已过期.*|.*追高放弃.*|.*时间离场.*', text):
        line = m.group(0).strip()
        if line and '#' not in line[:2] and '"""' not in line:
            print(f"{path}:{m.group(0)[:120]}")
