from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


class ReceiptVerdict(str, Enum):
    EXECUTED = "executed"
    IGNORED = "ignored"
    VETO = "veto"


class ReceiptEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    verdict: ReceiptVerdict
    size_filled: Optional[int] = None
    created_at: datetime

    @field_validator("signal_id")
    @classmethod
    def _validate_signal_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("signal_id must not be empty")
        return value.strip()

    @field_validator("size_filled")
    @classmethod
    def _validate_size(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("size_filled must not be negative")
        return value