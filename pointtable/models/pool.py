from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator


class PoolEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_date: date
    symbol: str
    theme_label: str

    @field_validator("symbol", "theme_label")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()