"""UNRELEASED: complete live 0–4 slot signed GET / SELECT-only guardian observation.

Bridges the dedicated independent V40 guardian's *read-only* transport and
restricted ledger role to the verified four-slot account+native MARKET SL
policy. This is the first network-capable four-slot WITNESS, not a trader.

The current deployed guardian MUST NOT import this module until separate
rollout and incident-scope review. It never opens, closes, amends, adopts
positions, creates incident scopes, writes SQL or issues a live mutation
permit. A successful native stop witness does not establish eventual fill
or cross-margin liquidation safety.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_four_slot_mexc_readonly import (
    _components,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator, _ledger_digest,
)
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_four_slot_guardian_policy import (
    audit_frozen_four_slot_native_stops,
)
from app.t100_v40_guardian_ledger_reader import PostgresV40GuardianLedgerReader


@dataclass(frozen=True)
class V40FourSlotSignedObservation:
    status: str
    account_flat_verified: bool = False
    owned_cross_positions: int = 0
    native_market_stops_verified: int = 0
    hedge_mode_verified: bool = False
    inventory_bookends_verified: bool = False
    immutable_ledger_bookends_verified: bool = False
    margin_and_liquidation_certified: bool = False
    native_stop_exits_certified: bool = False
    account_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0
    incident_alert: bool = True


async def _observe_verified_four_slot_sources(
    *,
    ledger: Any,
    venue: Any,
    completeness: Any,
    hedge_mode: int,
) -> V40FourSlotSignedObservation:
    """Offline-testable adapter, never a trading permit or safe-GET issuer.

    Only the outer guard admits actual dedicated guardian readers. This
    inner policy is intentionally nonauthorizing even when called with
    a synthetic fixture; no adapter can promote it into a live permit.
    """
    halt=lambda reason:V40FourSlotSignedObservation(status=reason)
    if type(hedge_mode) is not int or hedge_mode!=1:
        return halt("V40_FOUR_SLOT_SIGNED_HEDGE_MODE_UNVERIFIED")
    try:
        snapshot=await FourSlotSnapshotCoordinator(
            ledger=ledger,venue=venue,completeness=completeness,
        ).inspect()
    except Exception:
        return halt("V40_FOUR_SLOT_SIGNED_SNAPSHOT_RUNTIME_FAILED")
    if (
        snapshot.status=="FLAT_OBSERVED_NONAUTHORIZING"
        and snapshot.occupied_slots==0
        and snapshot.inventory is not None
        and snapshot.inventory.get("account_wide_completeness_verified") is True
        and snapshot.inventory.get(
            "critical_inventory_equal_across_witness_window"
        ) is True
        and snapshot.inventory.get("before_positions")==0
        and snapshot.inventory.get("after_positions")==0
        and snapshot.inventory.get("before_current_tpsl")==0
        and snapshot.inventory.get("after_current_tpsl")==0
        and snapshot.ledger_hash is not None
        and snapshot.critical_inventory_hash is not None
    ):
        return V40FourSlotSignedObservation(
            status="V40_FOUR_SLOT_SIGNED_ACCOUNT_FLAT_AND_LEDGER_CLEAN",
            account_flat_verified=True,hedge_mode_verified=True,
            inventory_bookends_verified=True,
            immutable_ledger_bookends_verified=True,
            incident_alert=False,
        )
    if (
        snapshot.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        or not 1<=snapshot.occupied_slots<=4
    ):
        return halt("V40_FOUR_SLOT_SIGNED_UNOWNED_OR_INCOMPLETE_ACCOUNT_HALT")
    try:
        # Read *after* the exact order/stop GET window, not from the first
        # pre-window ledger alone. An entry/amendment concurrent with the
        # signed GETs makes the hashes diverge and fails closed.
        second=await ledger.read_snapshot()
        if (
            _ledger_digest(second)!=snapshot.ledger_hash
            or type(getattr(venue,"last_positions",None)) is not list
            or type(getattr(venue,"last_stops",None)) is not list
        ):
            return halt("V40_FOUR_SLOT_SIGNED_LEDGER_OR_VENUE_DRIFT_HALT")
        audited=audit_frozen_four_slot_native_stops(
            snapshot=snapshot,ledger=second,
            account_positions=venue.last_positions,
            account_stops=venue.last_stops,
        )
    except Exception:
        return halt("V40_FOUR_SLOT_SIGNED_PROTECTED_AUDIT_FAILED")
    if not (
        audited.status==
            "V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING"
        and audited.all_owned_cross_positions_verified is True
        and audited.account_bookends_verified is True
        and audited.ledger_bookends_verified is True
        and audited.incident_alert is False
        and audited.protected_owned_positions==snapshot.occupied_slots
        and audited.matching_native_market_stops==snapshot.occupied_slots
    ):
        return halt("V40_FOUR_SLOT_SIGNED_UNPROTECTED_OR_UNKNOWN_OWNER_HALT")
    return V40FourSlotSignedObservation(
        status="V40_FOUR_SLOT_SIGNED_ONE_TO_FOUR_NATIVE_SL_PROTECTED_NONAUTHORIZING",
        owned_cross_positions=snapshot.occupied_slots,
        native_market_stops_verified=snapshot.occupied_slots,
        hedge_mode_verified=True,inventory_bookends_verified=True,
        immutable_ledger_bookends_verified=True,
        incident_alert=False,
    )


async def observe_dedicated_v40_four_slot_get_only(
    *, ledger: PostgresV40GuardianLedgerReader,
    signed_get: GetOnlyMexcTransport,
) -> V40FourSlotSignedObservation:
    """Strict gateway: signed GET-only exchange + independent SELECT-only DB.

    No ability to issue MEXC POST even after a positive observation. The
    caller must separately enroll account identity; never mix BOXWAVE.
    """
    halt=lambda reason:V40FourSlotSignedObservation(status=reason)
    if (
        type(ledger) is not PostgresV40GuardianLedgerReader
        or type(signed_get) is not GetOnlyMexcTransport
        or hasattr(signed_get,"_private_post")
    ):
        return halt("V40_FOUR_SLOT_SIGNED_READONLY_COMPONENTS_REQUIRED")
    try:
        mode=await ReadOnlyMexcProbe(signed_get).raw_position_mode()
    except Exception:
        return halt("V40_FOUR_SLOT_SIGNED_HEDGE_MODE_GET_FAILED_HALT")
    try:
        venue,completeness=_components(signed_get)
        return await _observe_verified_four_slot_sources(
            ledger=ledger,venue=venue,completeness=completeness,
            hedge_mode=mode,
        )
    except Exception:
        return halt("V40_FOUR_SLOT_SIGNED_RESTRICTED_READ_FAILED_HALT")
