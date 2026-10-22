from pathlib import Path

from src.config_env import load_project_env


def test_load_project_env_prefers_local_override(tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text('IWENCAI_API_KEY="base"\n', encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "IWENCAI_API_KEY=local\n", encoding="utf-8"
    )
    monkeypatch.delenv("IWENCAI_API_KEY", raising=False)

    assert load_project_env(project_root=tmp_path) is True
    import os

    assert os.environ["IWENCAI_API_KEY"] == "local"


def test_load_project_env_does_not_override_nonempty_value(tmp_path: Path):
    (tmp_path / ".env").write_text("IWENCAI_API_KEY=from-file\n", encoding="utf-8")
    import os

    os.environ["IWENCAI_API_KEY"] = "from-process"
    try:
        assert load_project_env(project_root=tmp_path) is False
        assert os.environ["IWENCAI_API_KEY"] == "from-process"
    finally:
        del os.environ["IWENCAI_API_KEY"]
