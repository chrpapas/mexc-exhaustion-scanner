"""GET-only, no-ledger-write MEXC emergency-close restart reconciliation.

An ACK is never proof of fill; a full CLOSE_SHORT exact-externalOid fill can
be established independently of MEXC's anomalous TP/SL triggerSide.

The current MEXC open-positions endpoint lacks an independently verifiable
account position total. Thus two absent reads cannot certify the owned
position is gone; the result deliberately leaves ledger UNKNOWN/SUBMITTING.
This module NEVER issues a POST, changes state, or touches manual positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, _validate_owned, close_intent_oid,
    _valid_id, _amount,
)
from app.t100_live_guardian_inventory_witness import (
    ReadOnlyGuardianCollector, GuardianWitnessHalted,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class CloseReconciliationHalted(RuntimeError):
    pass


class ReadOnlyCloseLedger(Protocol):
    async def read_state(self, external_oid: str, owned: OwnedShort) -> str: ...


@dataclass(frozen=True)
class CloseReconciliation:
    status: str
    order_filled_verified: bool = False
    owned_position_not_seen_twice: bool = False
    foreign_positions_preserved_observed: int = 0
    ledger_reconcile_only: bool = True
    live_authorization: bool = False


async def reconcile_close_read_only(
    *, owned: OwnedShort, actual_scope: str, ledger: ReadOnlyCloseLedger,
    probe: ReadOnlyMexcProbe,
) -> CloseReconciliation:
    _validate_owned(owned, actual_scope)
    if not isinstance(probe, ReadOnlyMexcProbe):
        raise CloseReconciliationHalted("STRICT_MEXC_SIGNED_GET_PROBE_REQUIRED")
    oid = close_intent_oid(owned)
    try:
        state = await ledger.read_state(oid, owned)
    except Exception as exc:
        raise CloseReconciliationHalted("CLOSE_LEDGER_NOT_IDENTITY_CERTIFIED") from exc
    if state not in {"SUBMITTING", "UNKNOWN", "VERIFIED"}:
        return CloseReconciliation("NO_SUBMISSION_CLAIM_TO_RECONCILE")

    try:
        ext = await probe.raw_order_by_external_oid(owned.symbol, oid)
    except Exception as exc:
        raise CloseReconciliationHalted("EXTERNAL_ORDER_GET_UNAVAILABLE") from exc
    if ext is None:
        # Absence from one API is NOT proof the POST failed or didn't fill.
        return CloseReconciliation("CLOSE_OID_NOT_FOUND_OUTCOME_UNKNOWN")

    if (str(ext.get("positionId")) != owned.position_id or
            ext.get("symbol") != owned.symbol or
            ext.get("externalOid") != oid or
            not _valid_id(ext.get("orderId"))):
        return CloseReconciliation("CLOSE_ORDER_ORIGIN_IDENTITY_MISMATCH")

    try:
        order = await probe.raw_order_by_id(str(ext["orderId"]))
    except Exception as exc:
        raise CloseReconciliationHalted("EXACT_CLOSE_ORDER_GET_UNAVAILABLE") from exc
    if (str(order.get("orderId")) != str(ext["orderId"]) or
            order.get("symbol") != owned.symbol or
            str(order.get("positionId")) != owned.position_id or
            order.get("externalOid") != oid):
        return CloseReconciliation("EXCHANGE_CLOSE_ORDER_IDENTITY_DRIFT")
    if not (
        order.get("side") == 2 and type(order.get("side")) is int and
        order.get("orderType") == 5 and type(order.get("orderType")) is int and
        order.get("openType") == 1 and type(order.get("openType")) is int and
        order.get("positionMode") == 1 and type(order.get("positionMode")) is int and
        order.get("state") == 3 and type(order.get("state")) is int and
        order.get("errorCode") == 0 and type(order.get("errorCode")) is int and
        _amount(order.get("dealVol")) == owned.contracts and
        _amount(order.get("vol")) == owned.contracts
    ):
        return CloseReconciliation("OWNED_CLOSE_ORDER_NOT_FULL_FILLED_VERIFIED")

    # Independent account-wide GET count bookends + position/stop census;
    # never treats manual positions as owned and never 'flats' the account.
    try:
        witness = await ReadOnlyGuardianCollector(probe).observe(
            owned, actual_scope,
        )
    except GuardianWitnessHalted as exc:
        return CloseReconciliation("FILLED_CLOSE_BUT_INVENTORY_WITNESS_UNVERIFIED",
                                   order_filled_verified=True)
    if witness.exact_owned_position_present:
        return CloseReconciliation(
            status="FILLED_CLOSE_BUT_OWNED_POSITION_STILL_VISIBLE",
            order_filled_verified=True,
            foreign_positions_preserved_observed=witness.foreign_position_count,
        )
    # The exact order finished fully, and owned position was not seen in two
    # stable reads, but absent snapshots alone lack independent completeness.
    # Do not transition journal to VERIFIED or authorize future entry.
    return CloseReconciliation(
        status="MATCHED_FILLED_CLOSE_OWNED_ABSENT_UNCERTIFIED_CENSUS",
        order_filled_verified=True,
        owned_position_not_seen_twice=True,
        foreign_positions_preserved_observed=witness.foreign_position_count,
    )
