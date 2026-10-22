"""Global test fixtures for isolation."""

from pathlib import Path

import pytest

from pointtable.db.session import set_database_path


@pytest.fixture
def tmp_db():
    db_path = Path.cwd() / 'test_pointtable_shadow.db'
    db_path.unlink(missing_ok=True)
    try:
        yield db_path
    finally:
        set_database_path(Path('unused'))
        db_path.unlink(missing_ok=True)
