"""T100-owned emergency-close policy and single-attempt journal boundary.

Opt-in integration component, NOT activated by current paper/Render workers.
No credentials, sockets, MEXC transport, or autonomous trading authorization.

Never adopts, closes, amends or cancels manual/foreign positions. Unknown
inventory MUST still count toward account-wide risk and blocks new entries.
Any emergency close requires exact durable entry ownership, two independently
complete matching exchange snapshots, a durable once-only intent and an
explicit call-site gate. An accepted POST is not a filled close.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Protocol


class OwnedGuardianHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedShort:
    scope_sha256: str
    symbol: str
    position_id: str
    entry_external_oid: str
    entry_order_id: str
    contracts: Decimal
    stop_order_id: str
    stop_price: Decimal
    checkpoint_version: int
    checkpoint_lifecycle: str = "OPEN"


@dataclass(frozen=True)
class GuardianObservation:
    status: str
    foreign_position_count: int = 0
    owned_position_observed: bool = False
    protected: bool = False
    close_candidate: bool = False
    live_authorization: bool = False


@dataclass(frozen=True)
class EmergencyCloseOutcome:
    status: str
    post_attempted: bool = False
    account_flat_verified: bool = False
    live_authorization: bool = False


def _valid_id(v: Any) -> bool:
    return (type(v) in (str, int) and bool(str(v)) and
            str(v).isascii() and str(v).isdecimal() and int(str(v)) > 0)


def _amount(v: Any) -> Decimal | None:
    if v is None or type(v) is bool:
        return None
    try:
        n = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return n if n.is_finite() and n > 0 else None


def _validate_owned(owned: OwnedShort, actual_scope: str) -> None:
    if not isinstance(owned, OwnedShort):
        raise OwnedGuardianHalted("DURABLE_OWNERSHIP_REQUIRED")
    if (not re.fullmatch("[a-f0-9]{64}", owned.scope_sha256 or "") or
            actual_scope != owned.scope_sha256 or
            not re.fullmatch(r"[A-Z0-9]+_USDT", owned.symbol or "") or
            not _valid_id(owned.position_id) or
            not _valid_id(owned.entry_order_id) or
            not re.fullmatch(r"t100[a-zA-Z0-9_]{8,60}",
                             owned.entry_external_oid or "") or
            not _valid_id(owned.stop_order_id) or
            _amount(owned.contracts) is None or
            _amount(owned.stop_price) is None or
            type(owned.checkpoint_version) is not int or
            owned.checkpoint_version < 1 or
            owned.checkpoint_lifecycle != "OPEN"):
        raise OwnedGuardianHalted("OWNED_SCOPE_CHECKPOINT_OR_IDENTITY_INVALID")


def assess(owned: OwnedShort, *, actual_scope: str, snapshot: Any,
           entry_order: Any) -> GuardianObservation:
    """One complete independent GET inventory; NEVER assume missing=flat.

    snapshot must come from a caller that has independently verified the
    account-wide completeness/count consistency of the venue responses.
    """
    _validate_owned(owned, actual_scope)
    if (type(snapshot) is not dict or
            snapshot.get("account_wide_complete") is not True or
            type(snapshot.get("positions")) is not list or
            type(snapshot.get("stops")) is not list or
            any(type(r) is not dict for r in
                snapshot["positions"] + snapshot["stops"])):
        return GuardianObservation(status="EXCHANGE_INVENTORY_NOT_CERTIFIED")

    positions = snapshot["positions"]
    stops = snapshot["stops"]
    matches = [p for p in positions if
               p.get("symbol") == owned.symbol and
               str(p.get("positionId")) == owned.position_id]
    foreign = len(positions) - len(matches)
    if len(matches) != 1:
        return GuardianObservation(
            status="OWNED_POSITION_ABSENT_OR_DUPLICATE_RECONCILE_ONLY",
            foreign_position_count=foreign,
        )
    p = matches[0]
    if (p.get("positionType") != 2 or
            str(p.get("state")) != "1" or
            _amount(p.get("holdVol")) != owned.contracts or
            p.get("openType") != 1):
        return GuardianObservation(
            status="OWNED_POSITION_STATE_OR_VOLUME_DRIFT",
            foreign_position_count=foreign,
        )
    if (type(entry_order) is not dict or
            entry_order.get("symbol") != owned.symbol or
            str(entry_order.get("positionId")) != owned.position_id or
            str(entry_order.get("orderId")) != owned.entry_order_id or
            entry_order.get("externalOid") != owned.entry_external_oid or
            entry_order.get("side") != 3 or
            entry_order.get("state") != 3 or
            _amount(entry_order.get("dealVol")) != owned.contracts):
        return GuardianObservation(
            status="ENTRY_OWNERSHIP_EVIDENCE_UNVERIFIED",
            foreign_position_count=foreign,
        )

    own_stops = [s for s in stops if
                 str(s.get("positionId")) == owned.position_id]
    if not own_stops:
        return GuardianObservation(
            status="OWNED_NATIVE_STOP_MISSING",
            foreign_position_count=foreign,
            owned_position_observed=True, close_candidate=True,
        )
    if len(own_stops) != 1:
        return GuardianObservation(
            status="OWNED_STOP_AMBIGUOUS_MANUAL_REVIEW",
            foreign_position_count=foreign, owned_position_observed=True,
        )
    stop = own_stops[0]
    if (str(stop.get("id") or stop.get("stopPlanOrderId")) !=
            owned.stop_order_id or
            stop.get("symbol") != owned.symbol or
            stop.get("positionType") != 2 or
            stop.get("state") != 1 or
            stop.get("isFinished") != 0 or
            str(stop.get("orderId")) != "0" or
            stop.get("stopLossType") != 0 or
            stop.get("volType") != 2 or
            stop.get("stopLossReverse") != 2 or
            stop.get("lossTrend") != 1 or
            stop.get("triggerSide") != 0 or
            stop.get("errorCode") != 0 or
            stop.get("profitLossVolType") != "SAME" or
            _amount(stop.get("stopLossPrice")) is None or
            _amount(stop.get("stopLossPrice")) > owned.stop_price):
        return GuardianObservation(
            status="OWNED_STOP_PROTECTIVENESS_UNVERIFIED_REVIEW",
            foreign_position_count=foreign, owned_position_observed=True,
        )
    return GuardianObservation(
        status="OWNED_POSITION_NATIVE_STOP_VERIFIED",
        foreign_position_count=foreign, owned_position_observed=True,
        protected=True,
    )


def close_intent_oid(owned: OwnedShort) -> str:
    """Stable only for this exact persisted position; never a random retry key."""
    identity = ":".join((
        owned.scope_sha256, owned.symbol, owned.position_id,
        owned.entry_external_oid, owned.entry_order_id,
    ))
    return "t100gc_" + hashlib.sha256(identity.encode()).hexdigest()[:22]


class CloseIntentLedger(Protocol):
    async def prepare_if_absent(self, external_oid: str,
                                owned: OwnedShort) -> bool: ...
    async def transition(self, external_oid: str, expected: str,
                         target: str) -> bool: ...


class CloseExchange(Protocol):
    async def close_short_once(self, *, external_oid: str,
                               symbol: str, position_id: str,
                               contracts: Decimal) -> Any: ...


def _same_critical_owned_snapshot(a: Any, b: Any, owned: OwnedShort) -> bool:
    """Require full exact inventory equality, no unrelated position drift."""
    if type(a) is not dict or type(b) is not dict:
        return False
    return (a == b and
            a.get("account_wide_complete") is True and
            type(a.get("positions")) is list and
            type(a.get("stops")) is list)


async def guarded_missing_stop_close(
    *, owned: OwnedShort, actual_scope: str,
    snapshot_a: Any, snapshot_b: Any,
    entry_order_a: Any, entry_order_b: Any,
    ledger: CloseIntentLedger, exchange: CloseExchange,
    mutation_enabled: bool = False,
) -> EmergencyCloseOutcome:
    """Potential real close; injected transport only; NO call if gated off.

    The caller must have independently performed two venue GET inventory
    witnesses and checked account scope; this helper verifies equality and
    ownership. It never selects a manual/unknown position for mutation.

    A journal transition to SUBMITTING is committed *before* calling POST;
    at most one attempt is permitted. Any uncertain response requires
    exchange reconciliation and operator review, NEVER automatic retry.
    """
    _validate_owned(owned, actual_scope)
    if not _same_critical_owned_snapshot(snapshot_a, snapshot_b, owned):
        return EmergencyCloseOutcome(status="TWO_SNAPSHOT_CONSISTENCY_UNVERIFIED")
    a = assess(owned, actual_scope=actual_scope,
               snapshot=snapshot_a, entry_order=entry_order_a)
    b = assess(owned, actual_scope=actual_scope,
               snapshot=snapshot_b, entry_order=entry_order_b)
    if (a.status != "OWNED_NATIVE_STOP_MISSING" or
            b.status != a.status or entry_order_a != entry_order_b):
        return EmergencyCloseOutcome(status="OWNED_MISSING_STOP_NOT_PROVEN")
    if mutation_enabled is not True:
        return EmergencyCloseOutcome(status="CLOSE_CANDIDATE_GATED_OFF")
    oid = close_intent_oid(owned)
    try:
        fresh = await ledger.prepare_if_absent(oid, owned)
        if fresh is not True:
            return EmergencyCloseOutcome(status="INTENT_ALREADY_EXISTS_RECONCILE_ONLY")
        claimed = await ledger.transition(oid, "PREPARED", "SUBMITTING")
        if claimed is not True:
            return EmergencyCloseOutcome(status="INTENT_CLAIM_FAILED_NO_POST")
    except Exception:
        return EmergencyCloseOutcome(status="DURABLE_LEDGER_FAILED_NO_POST")
    try:
        # Acknowledgement is NOT proof of fill or flatness. Never retry.
        await exchange.close_short_once(
            external_oid=oid, symbol=owned.symbol,
            position_id=owned.position_id, contracts=owned.contracts,
        )
    except Exception:
        try:
            await ledger.transition(oid, "SUBMITTING", "UNKNOWN")
        except Exception:
            pass
        return EmergencyCloseOutcome(
            status="CLOSE_POST_AMBIGUOUS_RECONCILE_ONLY", post_attempted=True,
        )
    # We don't mark VERIFIED until a *separate* durable read-only
    # exchange-order + flat-inventory reconciliation has succeeded.
    return EmergencyCloseOutcome(
        status="CLOSE_POST_ACK_UNVERIFIED_RECONCILE_ONLY", post_attempted=True,
    )
