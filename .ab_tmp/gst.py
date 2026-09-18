import subprocess
r = subprocess.run(['git', 'status', '--short'], capture_output=True, encoding='utf-8')
print(r.stdout)
