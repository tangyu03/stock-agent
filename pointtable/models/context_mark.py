from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ContextType(str, Enum):
    INDEX = "index"
    SECTOR = "sector"


class ContextStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ContextMark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    context_type: ContextType
    code: str
    level_low: float
    level_high: float
    label: str = ""
    status: ContextStatus = ContextStatus.ACTIVE
    triggered_at: Optional[datetime] = None
    created_at: datetime

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _validate_zone(self) -> "ContextMark":
        if self.level_low >= self.level_high:
            raise ValueError("level_low must be less than level_high")
        return self
