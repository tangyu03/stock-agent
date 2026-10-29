from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


class JournalRecordType(str, Enum):
    TRANSITION = "transition"
    ZONE_UPDATE = "zone_update"
    QUALIFIER = "qualifier"


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    from_status: str
    to_status: str
    reason: str = ""
    price: Optional[float] = None
    event: Optional[str] = None
    qualifier: Optional[str] = None
    old_zone: Optional[tuple[float, float]] = None
    new_zone: Optional[tuple[float, float]] = None
    record_type: JournalRecordType = JournalRecordType.TRANSITION
    event_source: str = 'live_settle'
    created_at: datetime
    occurred_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None
    zone_snapshot_id: Optional[str] = None
    run_id: Optional[str] = None
    code_version: Optional[str] = None
    input_snapshot_id: Optional[str] = None

    @field_validator("signal_id")
    @classmethod
    def _validate_signal_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("signal_id must not be empty")
        return value.strip()

    @field_validator("price")
    @classmethod
    def _validate_price(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and value < 0:
            raise ValueError("price must not be negative")
        return value
