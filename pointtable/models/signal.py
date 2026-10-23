from __future__ import annotations

from datetime import time, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class SignalRole(str, Enum):
    ENTRY = "entry"
    STOP = "stop"
    PROFIT = "profit"
    T = "t"
    RETEST_CONFIRM = "retest_confirm"


class SignalTag(str, Enum):
    ENTER = "enter"
    STOP = "stop"
    PROFIT = "profit"
    T = "t"
    RETEST_CONFIRM = "retest_confirm"
    INDEX_CONTEXT = "index_context"
    SECTOR_CONTEXT = "sector_context"
    TIME_TRIGGER = "time_trigger"


class SignalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class SignalTrend(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


class SignalValidPolicy(str, Enum):
    INTRADAY = "intraday"
    NEXT_DAY = "next_day"
    SAME_DAY = "same_day"
    FIXED_WINDOW = "fixed_window"
    UNTIL_STOPPED = "until_stopped"


_PARENT_REQUIRED_TAGS = {
    SignalTag.STOP,
    SignalTag.PROFIT,
    SignalTag.T,
    SignalTag.RETEST_CONFIRM,
}


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    symbol: str
    strategy_id: str
    side: SignalSide
    tag: SignalTag
    role: Optional[SignalRole] = None
    zone_low: float
    zone_high: float
    size_hint: Optional[int] = None
    premise: dict = {}
    break_qualifier: Optional[str] = None
    trend: SignalTrend
    activation: Optional[str] = None
    valid_policy: SignalValidPolicy
    status: SignalStatus = SignalStatus.ACTIVE
    status_reason: str = ""
    parent_id: Optional[str] = None
    created_at: datetime
    closed_at: Optional[datetime] = None
    trigger_window_start: Optional[time] = None
    trigger_window_end: Optional[time] = None

    @property
    def signal_role(self) -> SignalRole:
        if self.role is not None:
            return self.role
        return SignalRole(self.tag.value)

    @field_validator("symbol", "id", "strategy_id")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _validate_shape(self) -> "Signal":
        if self.role is None:
            self.role = {
                SignalTag.ENTER: SignalRole.ENTRY,
                SignalTag.STOP: SignalRole.STOP,
                SignalTag.PROFIT: SignalRole.PROFIT,
                SignalTag.T: SignalRole.T,
                SignalTag.RETEST_CONFIRM: SignalRole.RETEST_CONFIRM,
            }.get(self.tag)
        if self.role is None:
            raise ValueError("context and time rows must use ContextMark or event_calendar")
        if self.zone_low >= self.zone_high:
            raise ValueError("zone_low must be less than zone_high")
        if self.signal_role is SignalRole.RETEST_CONFIRM and self.side is not SignalSide.NEUTRAL:
            self.side = SignalSide.NEUTRAL
        if self.tag in _PARENT_REQUIRED_TAGS and not self.parent_id:
            raise ValueError(f"{self.tag.value} requires parent_id")
        if self.status in {SignalStatus.EXPIRED, SignalStatus.INVALIDATED} and not self.closed_at:
            raise ValueError(f"terminal status {self.status.value} requires closed_at")
        return self
