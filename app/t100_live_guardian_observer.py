"""Single-cycle, account-scoped, GET-ONLY T100-owned guardian observer.

It does NOT propose or issue emergency-close POSTs, amend/cancel stops,
transition the database, deploy Render workers, or adopt manual positions.
A missing active stop may represent a just-triggered native stop. Observer
never interprets absence as permission to issue another close.

This is an opt-in building block, not currently deployed on Render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, _validate_owned, _amount,
)
from app.t100_live_guardian_inventory_witness import (
    ReadOnlyGuardianCollector, GuardianWitnessHalted,
)
from app.t100_live_owned_position_finality import (
    verify_owned_position_finality, OwnedPositionFinalityHalted,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


@dataclass(frozen=True)
class GuardianObserverCycle:
    status: str
    foreign_position_count: int = 0
    owned_position_seen: bool = False
    native_market_stop_verified: bool = False
    alert: bool = True
    entry_allowed: bool = False
    live_authorization: bool = False
    close_allowed: bool = False
    mexc_orders_sent: bool = False


async def observe_owned_read_only(
    *, owned: OwnedShort, actual_scope: str,
    probe: ReadOnlyMexcProbe,
) -> GuardianObserverCycle:
    _validate_owned(owned, actual_scope)
    if type(probe) is not ReadOnlyMexcProbe:
        return GuardianObserverCycle("STRICT_SIGNED_GET_PROBE_REQUIRED")
    try:
        entry = await probe.raw_order_by_external_oid(
            owned.symbol, owned.entry_external_oid)
        if entry is None:
            return GuardianObserverCycle("ENTRY_DURABLE_OWNERSHIP_UNVERIFIED")
        if (str(entry.get("positionId")) != owned.position_id or
                str(entry.get("orderId")) != owned.entry_order_id or
                entry.get("symbol") != owned.symbol or
                entry.get("externalOid") != owned.entry_external_oid):
            return GuardianObserverCycle("ENTRY_ORIGIN_IDENTITY_MISMATCH")
        detailed = await probe.raw_order_by_id(owned.entry_order_id)
        if (str(detailed.get("positionId")) != owned.position_id or
                str(detailed.get("orderId")) != owned.entry_order_id or
                detailed.get("symbol") != owned.symbol or
                detailed.get("externalOid") != owned.entry_external_oid or
                type(detailed.get("side")) is not int or detailed["side"] != 3 or
                type(detailed.get("state")) is not int or detailed["state"] != 3 or
                type(detailed.get("openType")) is not int or detailed["openType"] != 1 or
                _amount(detailed.get("dealVol")) != owned.contracts):
            return GuardianObserverCycle("ENTRY_FILLED_OWNERSHIP_UNVERIFIED")
        witness = await ReadOnlyGuardianCollector(probe).observe(
            owned, actual_scope)
    except Exception:
        # Redacted. No response bytes, exchange IDs or credentials.
        return GuardianObserverCycle("SIGNED_GET_OR_INVENTORY_WITNESS_UNVERIFIED")

    foreign = witness.foreign_position_count
    if witness.exact_owned_position_present:
        if witness.owned_native_stop_protection_semantics_verified:
            return GuardianObserverCycle(
                status="OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED",
                foreign_position_count=foreign,
                owned_position_seen=True,
                native_market_stop_verified=True,
                alert=False,
            )
        if witness.owned_native_stop_present:
            return GuardianObserverCycle(
                status="OWNED_STOP_PRESENT_BUT_PROTECTION_UNVERIFIED",
                foreign_position_count=foreign,
                owned_position_seen=True,
            )
        # Stop could have triggered between inventory reads and history
        # availability, so absence can never authorize an emergency POST.
        try:
            rows = await probe.raw_stop_page(
                1, 100, is_finished=1,
            )
        except Exception:
            return GuardianObserverCycle(
                status="OWNED_STOP_MISSING_FINISHED_HISTORY_UNAVAILABLE",
                foreign_position_count=foreign, owned_position_seen=True,
            )
        matches = [
            row for row in rows
            if str(row.get("id") or row.get("stopPlanOrderId")) ==
               owned.stop_order_id and
               str(row.get("positionId")) == owned.position_id and
               row.get("symbol") == owned.symbol
        ]
        if len(matches) == 1 and matches[0].get("state") == 3:
            return GuardianObserverCycle(
                status="OWNED_STOP_MISSING_TERMINAL_TRIGGER_MAY_BE_IN_FLIGHT",
                foreign_position_count=foreign, owned_position_seen=True,
            )
        # A single page is not exhaustive. Unknown != no trigger.
        return GuardianObserverCycle(
            status="OWNED_STOP_MISSING_TRIGGER_STATUS_UNCERTAIN",
            foreign_position_count=foreign, owned_position_seen=True,
        )

    try:
        finality = await verify_owned_position_finality(
            owned=owned, actual_scope=actual_scope, probe=probe,
        )
    except OwnedPositionFinalityHalted:
        return GuardianObserverCycle(
            status="OWNED_POSITION_NOT_SEEN_HISTORY_UNVERIFIED",
            foreign_position_count=foreign,
        )
    if finality.closed_position_verified:
        return GuardianObserverCycle(
            status="OWNED_POSITION_CLOSED_IN_EXCHANGE_HISTORY",
            foreign_position_count=foreign, alert=False,
        )
    return GuardianObserverCycle(
        status="OWNED_POSITION_NOT_SEEN_HISTORY_NOT_CLOSED_PROVEN",
        foreign_position_count=foreign,
    )
