"""Public MEXC server-time / local-clock skew assessment."""
from __future__ import annotations

from dataclasses import dataclass


class ClockSkewHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ClockSkewReport:
    server_time_ms: int
    local_before_ms: int
    local_after_ms: int
    round_trip_ms: int
    outside_local_interval_ms: int
    within_limit: bool
    live_authorization: bool = False


def assess_clock_skew(
    *,
    server_time_ms: int,
    local_before_ms: int,
    local_after_ms: int,
    max_outside_interval_ms: int = 2000,
    max_round_trip_ms: int = 3000,
) -> ClockSkewReport:
    values=(server_time_ms,local_before_ms,local_after_ms)
    if any(isinstance(x,bool) or not isinstance(x,int) or x<=0 for x in values):
        raise ClockSkewHalted("CLOCK_INPUT_INVALID")
    if local_after_ms < local_before_ms:
        raise ClockSkewHalted("LOCAL_CLOCK_MOVED_BACKWARD")
    if (
        isinstance(max_outside_interval_ms,bool)
        or not isinstance(max_outside_interval_ms,int)
        or not 100 <= max_outside_interval_ms <= 5000
        or isinstance(max_round_trip_ms,bool)
        or not isinstance(max_round_trip_ms,int)
        or not 100 <= max_round_trip_ms <= 5000
    ):
        raise ClockSkewHalted("CLOCK_LIMIT_INVALID")

    rtt=local_after_ms-local_before_ms
    if server_time_ms < local_before_ms:
        outside=local_before_ms-server_time_ms
    elif server_time_ms > local_after_ms:
        outside=server_time_ms-local_after_ms
    else:
        outside=0

    within=(outside <= max_outside_interval_ms and rtt <= max_round_trip_ms)
    return ClockSkewReport(
        server_time_ms=server_time_ms,
        local_before_ms=local_before_ms,
        local_after_ms=local_after_ms,
        round_trip_ms=rtt,
        outside_local_interval_ms=outside,
        within_limit=within,
    )
