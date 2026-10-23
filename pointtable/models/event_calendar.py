from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventCalendar(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    event_type: str = 'time_trigger'
    code: str
    label: str = ''
    due_time: time
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
