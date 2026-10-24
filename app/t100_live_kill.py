"""Latched operational kill state for experimental T100 live components.

Pure state machine: no network, database, or exchange mutations.
There is deliberately no reset method. A new process/session plus operator
review is required after any trip.
"""
from __future__ import annotations

from dataclasses import dataclass


class KillSwitchTripped(RuntimeError):
    pass


ALLOWED_REASONS = frozenset({
    "MANUAL_KILL_SWITCH",
    "READ_FAILURE",
    "READ_TIMEOUT",
    "STALE_OBSERVATION",
    "OBSERVATION_GAP",
    "NONMONOTONIC_OBSERVATION",
    "INVENTORY_CHANGED_BETWEEN_READS",
    "INVENTORY_COMPLETENESS_UNVERIFIED",
    "UNRECOGNIZED_POSITION",
    "POSITION_DRIFT",
    "NATIVE_PROTECTION_LOST",
    "NATIVE_PROTECTION_DRIFT",
    "ORDER_LIFECYCLE_UNRESOLVED",
    "LEDGER_UNAVAILABLE",
    "RECOVERY_UNVERIFIED",
    "DAILY_LOSS_LIMIT",
    "PORTFOLIO_EXPOSURE_LIMIT",
    "POSITION_EXPOSURE_LIMIT",
})


@dataclass(frozen=True)
class KillSnapshot:
    tripped: bool
    first_reason: str | None
    trip_count: int
    entry_allowed: bool = False
    live_authorization: bool = False


class OperationalKillLatch:
    """One-way kill latch.

    Repeated trips retain the first cause while incrementing a diagnostic
    count. There is no reset/clear/enable operation.
    """

    def __init__(self) -> None:
        self._first_reason: str | None = None
        self._trip_count = 0

    @property
    def tripped(self) -> bool:
        return self._first_reason is not None

    @property
    def first_reason(self) -> str | None:
        return self._first_reason

    def trip(self, reason: str) -> KillSnapshot:
        if reason not in ALLOWED_REASONS:
            raise ValueError("UNKNOWN_KILL_REASON")
        self._trip_count += 1
        if self._first_reason is None:
            self._first_reason = reason
        return self.snapshot()

    def require_untripped(self) -> None:
        if self.tripped:
            raise KillSwitchTripped(
                f"OPERATIONAL_KILL_LATCHED:{self._first_reason}"
            )

    def snapshot(self) -> KillSnapshot:
        # This primitive is intentionally non-authorizing even when untripped.
        return KillSnapshot(
            tripped=self.tripped,
            first_reason=self._first_reason,
            trip_count=self._trip_count,
        )
