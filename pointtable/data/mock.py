from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class MockDataSource:
    '''Deterministic in-memory source for tests and offline dry runs.'''

    def __init__(self, rows: Mapping[str, Iterable[Mapping[str, Any]]] | None = None):
        self.rows = {name: [dict(row) for row in items] for name, items in (rows or {}).items()}

    def get(self, kind: str, **filters: Any) -> list[dict[str, Any]]:
        rows = self.rows.get(kind, [])
        return [row for row in rows if all(row.get(key) == value for key, value in filters.items())]

    def put(self, kind: str, rows: Iterable[Mapping[str, Any]]) -> None:
        self.rows[kind] = [dict(row) for row in rows]
