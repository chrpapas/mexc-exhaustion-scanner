"""MEXC signed-GET-only guardian inventory witness.

Runs two complete *API calls*, but intentionally does NOT claim account-wide
position-list completeness: MEXC documents open-order totals, not a separate
open-position total. Safe for operator observation and post-close research;
NEVER usable to authorize a POST directly. Returns no passwords or raw IDs.

Only app.t100_live_readonly_probe.ReadOnlyMexcProbe is used for exchange I/O.
No POST, no database write, no Render activity.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, OwnedGuardianHalted, _validate_owned,
    _same_critical_owned_snapshot,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_native_stop_execution_audit import assess_native_short_market_stop_row


class GuardianWitnessHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class GuardianInventoryWitness:
    status: str
    exact_owned_position_present: bool
    owned_native_stop_present: bool
    foreign_position_count: int
    open_positions_count: int
    stop_count: int
    open_order_count: int
    critical_snapshots_consistent: bool
    stop_total_reconciled: bool
    owned_native_stop_protection_semantics_verified: bool = False
    position_completeness_certified: bool = False
    emergency_close_authorized: bool = False
    live_authorization: bool = False


def _strict_positions(rows: Any) -> None:
    if type(rows) is not list or any(type(r) is not dict for r in rows):
        raise GuardianWitnessHalted("POSITIONS_MALFORMED")
    seen = set()
    for row in rows:
        symbol = row.get("symbol")
        pid = str(row.get("positionId") or "")
        if (not isinstance(symbol, str) or not symbol.endswith("_USDT") or
                not pid.isdecimal() or int(pid) <= 0 or
                row.get("positionType") not in (1, 2) or
                str(row.get("state")) != "1"):
            raise GuardianWitnessHalted("POSITION_IDENTITY_UNVERIFIED")
        key = (symbol, pid)
        if key in seen:
            raise GuardianWitnessHalted("DUPLICATE_POSITION_IDENTITY")
        seen.add(key)


def _strict_stops(rows: Any) -> None:
    if type(rows) is not list or any(type(r) is not dict for r in rows):
        raise GuardianWitnessHalted("STOPS_MALFORMED")
    seen = set()
    for row in rows:
        sid = str(row.get("id") or row.get("stopPlanOrderId") or "")
        pid = str(row.get("positionId") or "")
        if (not sid.isdecimal() or not pid.isdecimal() or
                int(sid) <= 0 or int(pid) <= 0):
            raise GuardianWitnessHalted("STOP_IDENTITY_UNVERIFIED")
        if sid in seen:
            raise GuardianWitnessHalted("DUPLICATE_STOP_IDENTITY")
        seen.add(sid)


class ReadOnlyGuardianCollector:
    def __init__(self, probe: ReadOnlyMexcProbe):
        if not isinstance(probe, ReadOnlyMexcProbe):
            raise GuardianWitnessHalted("STRICT_SIGNED_GET_PROBE_REQUIRED")
        self.probe = probe

    async def _cycle(self) -> dict[str, Any]:
        # Counts bookend inventory; the other four count categories matter.
        # Cannot assume a short unpaginated open-positions list is complete.
        before = await self.probe.raw_open_order_counts()
        positions = await self.probe.raw_open_positions()
        stops = await self.probe.raw_open_stops()
        after = await self.probe.raw_open_order_counts()
        if before != after:
            raise GuardianWitnessHalted("ORDER_COUNT_DRIFT_WITHIN_CYCLE")
        _strict_positions(positions)
        _strict_stops(stops)
        if len(stops) != before["stopOrderCount"]:
            raise GuardianWitnessHalted("ACCOUNT_STOP_COUNT_MISMATCH")
        if before["sumCount"] != sum(v for k, v in before.items() if k != "sumCount"):
            raise GuardianWitnessHalted("ACCOUNT_ORDER_COUNT_SUM_MISMATCH")
        return {
            "positions": positions, "stops": stops,
            "counts": before,
            # Explicitly FALSE. Caller cannot pass this to a POST-capable
            # guardian policy and claim certified account-wide completeness.
            "account_wide_complete": False,
        }

    async def observe(self, owned: OwnedShort, actual_scope: str) -> GuardianInventoryWitness:
        _validate_owned(owned, actual_scope)
        try:
            a = await self._cycle()
            b = await self._cycle()
        except GuardianWitnessHalted:
            raise
        except Exception as exc:
            raise GuardianWitnessHalted("SIGNED_GET_WITNESS_UNAVAILABLE") from exc
        # Compare critical fields, ignoring rapidly changing PnL.
        # This helper requires a certified-flag in each snapshot, so use a
        # private local copy only for equality analysis, NEVER return it.
        x = dict(a, account_wide_complete=True)
        y = dict(b, account_wide_complete=True)
        if not _same_critical_owned_snapshot(x, y, owned):
            raise GuardianWitnessHalted("TWO_CRITICAL_SNAPSHOTS_DIFFER")
        positions = b["positions"]
        stops = b["stops"]
        matches = [p for p in positions if
                   p["symbol"] == owned.symbol and
                   str(p["positionId"]) == owned.position_id]
        if len(matches) > 1:
            raise GuardianWitnessHalted("OWNED_POSITION_DUPLICATE")
        same_position_stops = [s for s in stops if
                               str(s.get("positionId")) == owned.position_id]
        if len(same_position_stops) > 1:
            raise GuardianWitnessHalted("OWNED_STOPS_DUPLICATE")
        matching_stop = [
            stop for stop in same_position_stops
            if str(stop.get("id") or stop.get("stopPlanOrderId")) == owned.stop_order_id
        ]
        verified_market_sl = False
        if len(matches) == 1 and len(matching_stop) == 1:
            row = matches[0]
            if (type(row.get("positionType")) is int and row["positionType"] == 2 and
                    str(row.get("state")) == "1" and
                    str(row.get("openType")) == "1"):
                audit = assess_native_short_market_stop_row(
                    stop=matching_stop[0], symbol=owned.symbol,
                    position_id=owned.position_id,
                    position_quantity=owned.contracts,
                    maximum_short_stop_price=owned.stop_price,
                )
                verified_market_sl = (
                    audit.active_native_full_volume_market_sl_observed
                    and str(row.get("holdVol")) == str(owned.contracts)
                )
        return GuardianInventoryWitness(
            status="READ_ONLY_WITNESS_POSITION_COMPLETENESS_UNATTESTED",
            exact_owned_position_present=len(matches) == 1,
            owned_native_stop_present=any(
                str(s.get("id") or s.get("stopPlanOrderId")) == owned.stop_order_id
                for s in same_position_stops
            ),
            foreign_position_count=len(positions) - len(matches),
            open_positions_count=len(positions),
            stop_count=len(stops),
            open_order_count=b["counts"]["sumCount"],
            critical_snapshots_consistent=True,
            stop_total_reconciled=True,
            owned_native_stop_protection_semantics_verified=verified_market_sl,
        )
