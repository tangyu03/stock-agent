"""加载项目本地环境变量，支持 .env 与 .env.local。"""
from pathlib import Path
import os
from typing import Iterable, Optional


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_project_env(
    project_root: Optional[Path] = None,
    filenames: Iterable[str] = (".env", ".env.local"),
) -> bool:
    """加载缺失的环境变量；已存在的非空变量不会被覆盖。"""
    root = project_root or Path(__file__).parent.parent
    values: dict[str, str] = {}
    for filename in filenames:
        values.update(_parse_env_file(root / filename))
    loaded = False
    for key, value in values.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded = True
    return loaded
