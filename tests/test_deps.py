from pathlib import Path


def test_core_has_no_data_db_or_notify_imports():
    forbidden = ('from ..data', 'from ..db', 'from ..notify', 'import pointtable.data', 'import pointtable.db', 'import pointtable.notify')
    files = list(Path('pointtable/core').glob('*.py'))
    assert files
    for path in files:
        text = path.read_text(encoding='utf-8')
        assert not any(term in text for term in forbidden), path
