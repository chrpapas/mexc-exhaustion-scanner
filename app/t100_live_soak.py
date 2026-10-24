"""Bounded read-only soak evidence for T100 live-readiness work.

Two deliberately separate layers:

1) RawDiscoverySoakRunner
   Can run against today's strict GET-only MEXC probe. It verifies repeated
   double-read stability but NEVER claims endpoint completeness, protection,
   account atomicity, or entry permission.

2) OperationalSoakController
   Consumes independently complete, expected-account snapshots and uses the
   fail-closed shadow reconciler. One failure permanently trips an operational
   kill latch. Even a fully successful soak remains non-authorizing.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from app.t100_live_consistency import (
    ConsistencyHalted,
    InventoryRead,
    double_read_projected_inventory,
)
from app.t100_live_kill import OperationalKillLatch
from app.t100_live_shadow_session import (
    ShadowObservation,
    ShadowSession,
    ShadowSessionHalted,
)


class SoakHalted(RuntimeError):
    pass


class RawInventoryProbe(Protocol):
    async def raw_open_positions(self) -> list[dict[str, Any]]: ...
    async def raw_open_stops(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DiscoverySoakConfig:
    samples: int = 12
    interval_seconds: float = 5.0
    max_runtime_seconds: float = 120.0
    max_pair_separation_ms: int = 2000

    def __post_init__(self) -> None:
        if (
            type(self.samples) is not int
            or not 2 <= self.samples <= 240
        ):
            raise ValueError("INVALID_SOAK_SAMPLE_COUNT")
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or not 0 <= float(self.interval_seconds) <= 60
        ):
            raise ValueError("INVALID_SOAK_INTERVAL")
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not isinstance(self.max_runtime_seconds, (int, float))
            or not 1 <= float(self.max_runtime_seconds) <= 3600
        ):
            raise ValueError("INVALID_SOAK_RUNTIME")
        if (
            type(self.max_pair_separation_ms) is not int
            or not 1 <= self.max_pair_separation_ms <= 3000
        ):
            raise ValueError("INVALID_SOAK_PAIR_SEPARATION")


@dataclass(frozen=True)
class DiscoverySoakReport:
    requested_samples: int
    completed_samples: int
    consistent_samples: int
    position_presence_samples: int
    current_tpsl_presence_samples: int
    position_without_current_tpsl_samples: int
    cross_sample_inventory_count_changes: int
    full_raw_drift_samples: int
    changed_position_field_names: tuple[str, ...]
    changed_stop_field_names: tuple[str, ...]
    elapsed_ms: int
    kill_tripped: bool
    kill_reason: str | None
    status: str
    raw_inventory_completeness_verified: bool = False
    exchange_atomicity_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


class RawDiscoverySoakRunner:
    """Bounded GET-only repeated-read discovery.

    A stable pair means only that two consecutive raw responses matched.
    Cross-sample inventory transitions are counted, not treated as corruption,
    because this discovery layer has no authoritative local lifecycle model.
    """

    def __init__(self, probe: RawInventoryProbe, config: DiscoverySoakConfig):
        self.probe = probe
        self.config = config
        self.kill = OperationalKillLatch()

    async def run(
        self,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> DiscoverySoakReport:
        start_ns = monotonic_ns()
        completed = consistent = 0
        pos_present = stop_present = pos_without_stop = 0
        count_changes = 0
        full_raw_drift_samples = 0
        changed_position_fields: set[str] = set()
        changed_stop_fields: set[str] = set()
        last_counts: tuple[int, int] | None = None
        last_observed_ms = -1

        for index in range(self.config.samples):
            elapsed_ms = (monotonic_ns() - start_ns) // 1_000_000
            if elapsed_ms > int(self.config.max_runtime_seconds * 1000):
                self.kill.trip("READ_TIMEOUT")
                break

            last_read: InventoryRead | None = None

            async def reader() -> InventoryRead:
                nonlocal last_read, last_observed_ms
                try:
                    positions = await self.probe.raw_open_positions()
                    stops = await self.probe.raw_open_stops()
                except Exception as exc:
                    raise SoakHalted("RAW_SOAK_READ_FAILED") from exc
                observed_ms = monotonic_ns() // 1_000_000
                if observed_ms <= last_observed_ms:
                    observed_ms = last_observed_ms + 1
                last_observed_ms = observed_ms
                last_read = InventoryRead(
                    positions=positions,
                    stops=stops,
                    observed_at_ms=observed_ms,
                    positions_complete=False,
                    stops_complete=False,
                )
                return last_read

            try:
                evidence = await double_read_projected_inventory(
                    reader,
                    max_separation_ms=self.config.max_pair_separation_ms,
                )
                if not evidence.full_raw_equal:
                    full_raw_drift_samples += 1
                    changed_position_fields.update(
                        evidence.changed_position_field_names
                    )
                    changed_stop_fields.update(
                        evidence.changed_stop_field_names
                    )
            except ConsistencyHalted as exc:
                reason = (
                    "INVENTORY_CHANGED_BETWEEN_READS"
                    if "CRITICAL_INVENTORY_CHANGED_BETWEEN_READS" in str(exc)
                    else "READ_FAILURE"
                )
                self.kill.trip(reason)
                break
            except Exception:
                self.kill.trip("READ_FAILURE")
                break

            if last_read is None:
                self.kill.trip("READ_FAILURE")
                break

            completed += 1
            consistent += 1
            counts = (len(last_read.positions), len(last_read.stops))
            if counts[0] > 0:
                pos_present += 1
            if counts[1] > 0:
                stop_present += 1
            if counts[0] > 0 and counts[1] == 0:
                pos_without_stop += 1
            if last_counts is not None and counts != last_counts:
                count_changes += 1
            last_counts = counts

            if index + 1 < self.config.samples:
                await sleep(float(self.config.interval_seconds))

        elapsed_ms = max(0, (monotonic_ns() - start_ns) // 1_000_000)
        status = (
            "COMPLETED_READ_ONLY_DISCOVERY"
            if completed == self.config.samples and not self.kill.tripped
            else "HALTED_READ_ONLY_DISCOVERY"
        )
        snapshot = self.kill.snapshot()
        return DiscoverySoakReport(
            requested_samples=self.config.samples,
            completed_samples=completed,
            consistent_samples=consistent,
            position_presence_samples=pos_present,
            current_tpsl_presence_samples=stop_present,
            position_without_current_tpsl_samples=pos_without_stop,
            cross_sample_inventory_count_changes=count_changes,
            full_raw_drift_samples=full_raw_drift_samples,
            changed_position_field_names=tuple(sorted(changed_position_fields)),
            changed_stop_field_names=tuple(sorted(changed_stop_fields)),
            elapsed_ms=elapsed_ms,
            kill_tripped=snapshot.tripped,
            kill_reason=snapshot.first_reason,
            status=status,
        )


@dataclass(frozen=True)
class OperationalSoakReport:
    observations: int
    minimum_observations: int
    soak_threshold_met: bool
    kill_tripped: bool
    kill_reason: str | None
    status: str
    entry_allowed: bool = False
    live_authorization: bool = False


class OperationalSoakController:
    """Latched controller around independently complete shadow snapshots."""

    def __init__(
        self,
        *,
        minimum_observations: int = 100,
        max_gap_ms: int = 60_000,
    ) -> None:
        if (
            type(minimum_observations) is not int
            or not 2 <= minimum_observations <= 100_000
        ):
            raise ValueError("INVALID_MINIMUM_SOAK_OBSERVATIONS")
        self.minimum_observations = minimum_observations
        self.session = ShadowSession(max_gap_ms=max_gap_ms)
        self.kill = OperationalKillLatch()
        self._observations = 0
        self._last_at: int | None = None
        self._max_gap_ms = max_gap_ms

    def _trip(self, reason: str) -> None:
        self.kill.trip(reason)

    def observe(self, item: ShadowObservation) -> OperationalSoakReport:
        self.kill.require_untripped()

        if item.positions_complete is not True or item.stops_complete is not True:
            self._trip("INVENTORY_COMPLETENESS_UNVERIFIED")
            raise SoakHalted("OPERATIONAL_SOAK_KILLED:INVENTORY_COMPLETENESS_UNVERIFIED")

        if (
            not isinstance(item.observed_at_ms, int)
            or isinstance(item.observed_at_ms, bool)
            or not isinstance(item.now_ms, int)
            or isinstance(item.now_ms, bool)
        ):
            self._trip("STALE_OBSERVATION")
            raise SoakHalted("OPERATIONAL_SOAK_KILLED:STALE_OBSERVATION")

        if not 0 <= item.now_ms - item.observed_at_ms <= 1500:
            self._trip("STALE_OBSERVATION")
            raise SoakHalted("OPERATIONAL_SOAK_KILLED:STALE_OBSERVATION")

        if (
            not isinstance(item.positions, list)
            or not isinstance(item.stops, list)
        ):
            self._trip("RECOVERY_UNVERIFIED")
            raise SoakHalted(
                "OPERATIONAL_SOAK_KILLED:RECOVERY_UNVERIFIED"
            )

        try:
            expected_count = len(item.expected)
        except Exception as exc:
            self._trip("RECOVERY_UNVERIFIED")
            raise SoakHalted(
                "OPERATIONAL_SOAK_KILLED:RECOVERY_UNVERIFIED"
            ) from exc
        if len(item.positions) != expected_count:
            self._trip("POSITION_DRIFT")
            raise SoakHalted("OPERATIONAL_SOAK_KILLED:POSITION_DRIFT")
        if len(item.stops) < expected_count:
            self._trip("NATIVE_PROTECTION_LOST")
            raise SoakHalted(
                "OPERATIONAL_SOAK_KILLED:NATIVE_PROTECTION_LOST"
            )
        if len(item.stops) > expected_count:
            self._trip("NATIVE_PROTECTION_DRIFT")
            raise SoakHalted(
                "OPERATIONAL_SOAK_KILLED:NATIVE_PROTECTION_DRIFT"
            )

        if self._last_at is not None:
            if item.observed_at_ms <= self._last_at:
                self._trip("NONMONOTONIC_OBSERVATION")
                raise SoakHalted(
                    "OPERATIONAL_SOAK_KILLED:NONMONOTONIC_OBSERVATION"
                )
            if item.observed_at_ms - self._last_at > self._max_gap_ms:
                self._trip("OBSERVATION_GAP")
                raise SoakHalted("OPERATIONAL_SOAK_KILLED:OBSERVATION_GAP")

        try:
            self.session.observe(item)
        except ShadowSessionHalted as exc:
            message = repr(exc.__cause__) if exc.__cause__ is not None else str(exc)
            if "STOP" in message or "PROTECTION" in message:
                reason = "NATIVE_PROTECTION_DRIFT"
            elif "POSITION" in message or "INVENTORY_COUNT" in message:
                reason = "POSITION_DRIFT"
            elif "PAGINATION" in message or "COMPLETENESS" in message:
                reason = "INVENTORY_COMPLETENESS_UNVERIFIED"
            elif "STALE" in message:
                reason = "STALE_OBSERVATION"
            else:
                reason = "RECOVERY_UNVERIFIED"
            self._trip(reason)
            raise SoakHalted(f"OPERATIONAL_SOAK_KILLED:{reason}") from exc

        self._last_at = item.observed_at_ms
        self._observations += 1
        threshold = self._observations >= self.minimum_observations
        snapshot = self.kill.snapshot()
        return OperationalSoakReport(
            observations=self._observations,
            minimum_observations=self.minimum_observations,
            soak_threshold_met=threshold,
            kill_tripped=snapshot.tripped,
            kill_reason=snapshot.first_reason,
            status=(
                "SOAK_THRESHOLD_MET_NON_AUTHORIZING"
                if threshold
                else "SOAK_IN_PROGRESS_NON_AUTHORIZING"
            ),
        )

    def report(self) -> OperationalSoakReport:
        snapshot = self.kill.snapshot()
        threshold = self._observations >= self.minimum_observations
        return OperationalSoakReport(
            observations=self._observations,
            minimum_observations=self.minimum_observations,
            soak_threshold_met=threshold and not snapshot.tripped,
            kill_tripped=snapshot.tripped,
            kill_reason=snapshot.first_reason,
            status=(
                "SOAK_KILLED"
                if snapshot.tripped
                else "SOAK_THRESHOLD_MET_NON_AUTHORIZING"
                if threshold
                else "SOAK_IN_PROGRESS_NON_AUTHORIZING"
            ),
        )
