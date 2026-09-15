import subprocess
import sys
from pathlib import Path
patch = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
exe = r"C:\Users\15831\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
result = subprocess.run([exe, "--codex-run-as-apply-patch", patch], encoding="utf-8")
raise SystemExit(result.returncode)
