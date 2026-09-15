"""Canonical signal status enum and count labels."""

TRIGGERED = "valid"
FILLED = "filled"
FROZEN = "frozen"
STRUCT_CANCEL = "struct_cancel"
EXPIRED = "expired"
CHASE_ABANDON = "chase_abandon"
SIG_STOP = "sig_stop"
SIG_TARGET = "sig_target"
TIME_EXIT = "time_exit"


MATCH_COUNT_LABELS = {
    TRIGGERED: "等待回踩",
    FILLED: "成交",
    FROZEN: "冻结",
    STRUCT_CANCEL: "结构失败撤单",
    EXPIRED: "到期",
    CHASE_ABANDON: "追高放弃",
    SIG_STOP: "信号止损",
    SIG_TARGET: "信号止盈",
    TIME_EXIT: "时间离场",
}


def canonical_match_status(status: str, invalid_reason: str = "") -> str:
    """Map legacy rows to the match-count enum without rewriting stored state."""
    value = str(status or "valid")
    if value == "triggered":
        return FILLED
    if value != "invalidated":
        return value
    reason = str(invalid_reason or "")
    if (
        "信号止损" in reason
        or "先触及止损价" in reason
        or "信号口径认错" in reason
    ):
        return SIG_STOP
    return STRUCT_CANCEL
