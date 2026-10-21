from pathlib import Path
path = Path('src/push/pushplus.py')
text = path.read_text(encoding='utf-8')
replacements = [
    ("        from .templates import (\n            _esc,\n            build_report_global_context,\n            pattern_summary_line,\n            render_report_global_context,\n            render_action_summary,\n            render_environment_overview,\n            render_entry_signal,\n", "        from .templates import (\n            _esc,\n            render_action_summary,\n            render_entry_signal,\n"),
]
for old, new in replacements:
    if old not in text:
        raise SystemExit('context missing')
    text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
