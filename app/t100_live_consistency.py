"""Conservative read-only repeated-inventory consistency check.

A double-read with identical canonical rows reduces (but does not eliminate)
torn snapshots. This is NOT exchange atomicity, account protection, or
authorization for order submission.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.t100_live_snapshot import validate_inventory_snapshot


class ConsistencyHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class InventoryRead:
    positions: list[dict[str, Any]]
    stops: list[dict[str, Any]]
    observed_at_ms: int
    positions_complete: bool
    stops_complete: bool


@dataclass(frozen=True)
class ConsistencyEvidence:
    mode: str
    first_observed_at_ms: int
    second_observed_at_ms: int
    positions: int
    stops: int
    fingerprint_sha256: str


def _fingerprint(read: InventoryRead) -> str:
    try:
        # Canonical sorting allows venue-independent ordering while rejecting
        # unserializable values rather than stringifying arbitrary objects.
        p = sorted(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                   for row in read.positions)
        s = sorted(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                   for row in read.stops)
        payload = json.dumps({"positions": p, "stops": s}, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ConsistencyHalted("NONCANONICAL_INVENTORY_ROW") from exc
    return hashlib.sha256(payload.encode()).hexdigest()


async def double_read_inventory(
    reader: Callable[[], Awaitable[InventoryRead]],
    *, now_ms: Callable[[], int],
    max_separation_ms: int = 1000,
) -> ConsistencyEvidence:
    if type(max_separation_ms) is not int or not 0 < max_separation_ms <= 1500:
        raise ValueError("INVALID_MAX_SEPARATION")
    try:
        first = await reader()
        if not isinstance(first, InventoryRead):
            raise ConsistencyHalted("INVENTORY_READ_TYPE")
        validate_inventory_snapshot(
            positions=first.positions, stops=first.stops,
            observed_at_ms=first.observed_at_ms, now_ms=now_ms(),
            positions_complete=first.positions_complete,
            stops_complete=first.stops_complete,
        )
        first_hash = _fingerprint(first)
        second = await reader()
        if not isinstance(second, InventoryRead):
            raise ConsistencyHalted("INVENTORY_READ_TYPE")
        validate_inventory_snapshot(
            positions=second.positions, stops=second.stops,
            observed_at_ms=second.observed_at_ms, now_ms=now_ms(),
            positions_complete=second.positions_complete,
            stops_complete=second.stops_complete,
        )
        second_hash = _fingerprint(second)
    except Exception as exc:
        raise ConsistencyHalted("INVENTORY_DOUBLE_READ_UNVERIFIED") from exc
    if type(first.observed_at_ms) is not int or type(second.observed_at_ms) is not int:
        raise ConsistencyHalted("INVALID_OBSERVATION_TIMESTAMPS")
    delta = second.observed_at_ms - first.observed_at_ms
    if delta <= 0 or delta > max_separation_ms:
        raise ConsistencyHalted("INVENTORY_READS_NOT_CONSECUTIVE")
    if first_hash != second_hash:
        raise ConsistencyHalted("INVENTORY_CHANGED_BETWEEN_READS")
    return ConsistencyEvidence(
        "SHADOW_ONLY", first.observed_at_ms, second.observed_at_ms,
        len(second.positions), len(second.stops), second_hash,
    )



@dataclass(frozen=True)
class RawConsistencyEvidence:
    """Consistency only; explicitly does not establish collection completeness."""
    mode: str
    first_observed_at_ms: int
    second_observed_at_ms: int
    positions: int
    stops: int


async def double_read_raw_inventory(
    reader: Callable[[], Awaitable[InventoryRead]],
    *, max_separation_ms: int = 2000,
) -> RawConsistencyEvidence:
    """Compare two strict raw inventory reads without asserting completeness.

    This is appropriate during venue contract discovery: identical snapshots
    reduce torn-read risk but do not prove that an endpoint returned every
    position/stop, nor that MEXC provides an atomic account snapshot.
    """
    if type(max_separation_ms) is not int or not 0 < max_separation_ms <= 3000:
        raise ValueError("INVALID_MAX_SEPARATION")
    try:
        first = await reader()
        if not isinstance(first, InventoryRead):
            raise ConsistencyHalted("INVENTORY_READ_TYPE")
        if not isinstance(first.positions, list) or not isinstance(first.stops, list):
            raise ConsistencyHalted("INVENTORY_READ_MALFORMED")
        first_hash = _fingerprint(first)

        second = await reader()
        if not isinstance(second, InventoryRead):
            raise ConsistencyHalted("INVENTORY_READ_TYPE")
        if not isinstance(second.positions, list) or not isinstance(second.stops, list):
            raise ConsistencyHalted("INVENTORY_READ_MALFORMED")
        second_hash = _fingerprint(second)
    except Exception as exc:
        raise ConsistencyHalted("RAW_INVENTORY_DOUBLE_READ_UNVERIFIED") from exc

    if type(first.observed_at_ms) is not int or type(second.observed_at_ms) is not int:
        raise ConsistencyHalted("INVALID_OBSERVATION_TIMESTAMPS")
    delta = second.observed_at_ms - first.observed_at_ms
    if delta <= 0 or delta > max_separation_ms:
        raise ConsistencyHalted("RAW_INVENTORY_READS_NOT_CONSECUTIVE")
    if first_hash != second_hash:
        raise ConsistencyHalted("RAW_INVENTORY_CHANGED_BETWEEN_READS")
    return RawConsistencyEvidence(
        "READ_ONLY_DISCOVERY", first.observed_at_ms, second.observed_at_ms,
        len(second.positions), len(second.stops),
    )

CRITICAL_POSITION_FIELDS = (
    "positionId", "id", "symbol", "holdVol", "positionType", "openType",
    "state", "frozenVol", "closeVol", "holdAvgPrice",
    "holdAvgPriceFullyScale", "openAvgPrice", "openAvgPriceFullyScale",
    "newOpenAvgPrice", "newCloseAvgPrice", "leverage", "im", "oim",
    "autoAddIm", "positionDrop",
)

CRITICAL_STOP_FIELDS = (
    "id", "stopPlanOrderId", "orderId", "positionId", "symbol",
    "state", "isFinished", "triggerSide", "positionType",
    "lossTrend", "profitTrend", "stopLossPrice", "takeProfitPrice",
    "vol", "volType", "profitLossVolType", "takeProfitVol", "stopLossVol",
    "priceProtect", "takeProfitReverse", "stopLossReverse",
    "takeProfitType", "takeProfitOrderPrice", "stopLossType",
    "stopLossOrderPrice",
)


@dataclass(frozen=True)
class ProjectedConsistencyEvidence:
    """Safety-critical stability plus full-payload drift diagnostics.

    Critical equality is the gate. Full raw equality is diagnostic only,
    because MEXC position responses contain naturally changing mark-to-market
    and fee/funding fields.
    """
    mode: str
    first_observed_at_ms: int
    second_observed_at_ms: int
    positions: int
    stops: int
    critical_inventory_equal: bool
    full_raw_equal: bool
    changed_position_field_names: tuple[str, ...]
    changed_stop_field_names: tuple[str, ...]


def _critical_rows(
    rows: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        projected.append({key: row[key] for key in fields if key in row})
    return projected


def _changed_field_names(
    first_rows: list[dict[str, Any]],
    second_rows: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Return schema field names whose multiset of values changed; never values."""
    names = set()
    all_keys = {
        key
        for row in first_rows + second_rows
        for key in row
        if isinstance(key, str)
    }
    for key in all_keys:
        try:
            a = sorted(
                json.dumps(row.get(key), sort_keys=True, separators=(",", ":"),
                           allow_nan=False)
                for row in first_rows
            )
            b = sorted(
                json.dumps(row.get(key), sort_keys=True, separators=(",", ":"),
                           allow_nan=False)
                for row in second_rows
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConsistencyHalted("NONCANONICAL_INVENTORY_ROW") from exc
        if a != b:
            names.add(key)
    return tuple(sorted(names))


async def double_read_projected_inventory(
    reader: Callable[[], Awaitable[InventoryRead]],
    *,
    max_separation_ms: int = 2000,
) -> ProjectedConsistencyEvidence:
    """Require safety-critical equality while preserving raw-drift evidence.

    This does not establish endpoint completeness or exchange atomicity.
    """
    if type(max_separation_ms) is not int or not 0 < max_separation_ms <= 3000:
        raise ValueError("INVALID_MAX_SEPARATION")
    try:
        first = await reader()
        second = await reader()
        if not isinstance(first, InventoryRead) or not isinstance(second, InventoryRead):
            raise ConsistencyHalted("INVENTORY_READ_TYPE")
        if (
            not isinstance(first.positions, list)
            or not isinstance(first.stops, list)
            or not isinstance(second.positions, list)
            or not isinstance(second.stops, list)
            or any(not isinstance(row, dict) for row in first.positions + first.stops)
            or any(not isinstance(row, dict) for row in second.positions + second.stops)
        ):
            raise ConsistencyHalted("INVENTORY_READ_MALFORMED")

        full_first = _fingerprint(first)
        full_second = _fingerprint(second)
        critical_first = InventoryRead(
            _critical_rows(first.positions, CRITICAL_POSITION_FIELDS),
            _critical_rows(first.stops, CRITICAL_STOP_FIELDS),
            first.observed_at_ms, False, False,
        )
        critical_second = InventoryRead(
            _critical_rows(second.positions, CRITICAL_POSITION_FIELDS),
            _critical_rows(second.stops, CRITICAL_STOP_FIELDS),
            second.observed_at_ms, False, False,
        )
        critical_first_hash = _fingerprint(critical_first)
        critical_second_hash = _fingerprint(critical_second)
        changed_positions = _changed_field_names(first.positions, second.positions)
        changed_stops = _changed_field_names(first.stops, second.stops)
    except Exception as exc:
        if isinstance(exc, ConsistencyHalted):
            raise
        raise ConsistencyHalted(
            "PROJECTED_INVENTORY_DOUBLE_READ_UNVERIFIED"
        ) from exc

    if type(first.observed_at_ms) is not int or type(second.observed_at_ms) is not int:
        raise ConsistencyHalted("INVALID_OBSERVATION_TIMESTAMPS")
    delta = second.observed_at_ms - first.observed_at_ms
    if delta <= 0 or delta > max_separation_ms:
        raise ConsistencyHalted("PROJECTED_INVENTORY_READS_NOT_CONSECUTIVE")
    if critical_first_hash != critical_second_hash:
        raise ConsistencyHalted("CRITICAL_INVENTORY_CHANGED_BETWEEN_READS")

    return ProjectedConsistencyEvidence(
        mode="READ_ONLY_DISCOVERY_PROJECTED",
        first_observed_at_ms=first.observed_at_ms,
        second_observed_at_ms=second.observed_at_ms,
        positions=len(second.positions),
        stops=len(second.stops),
        critical_inventory_equal=True,
        full_raw_equal=full_first == full_second,
        changed_position_field_names=changed_positions,
        changed_stop_field_names=changed_stops,
    )

