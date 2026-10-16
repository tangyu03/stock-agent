from pathlib import Path
import subprocess
patch = Path(".ab_tmp/patch_nan").read_text(encoding="utf-8-sig").replace("\ufeff", "")
codex = Path.home() / "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
raise SystemExit(subprocess.run([str(codex), "--codex-run-as-apply-patch", patch], text=True, encoding="utf-8").returncode)
