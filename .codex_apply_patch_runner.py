import subprocess
from pathlib import Path
exe = r'C:\Users\15831\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
patch = """*** Begin Patch
*** Update File: pointtable/notify/render.py
@@
         elif float(close) < float(ma5):
-            parts.append('MA5破了，明天别幻想。')
+            parts.append(
+                f'收{_price(close)}，已破MA5（{_price(ma5)}）'
+                f'{distance * 100:.2f}%，明天别幻想。'
+            )
*** End Patch"""
run = subprocess.run([exe, '--codex-run-as-apply-patch', patch], capture_output=True, text=True, encoding='utf-8')
print(run.stdout)
print(run.stderr)
raise SystemExit(run.returncode)
