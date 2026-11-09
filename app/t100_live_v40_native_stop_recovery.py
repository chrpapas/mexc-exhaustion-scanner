"""Strict V40 read-only native-stop amendment recovery, no POST or DB writes.

Every recovery attempt reconciles the *durable* amendment journal + checkpoint
against two stable full-account signed-GET inventory bookends and a restricted
READ ONLY REPEATABLE READ ledger. It NEVER marks an amendment VERIFIED,
changes a checkpoint, or retries a possibly-accepted stop POST.

- PREPARED is not evidence that an exchange mutation was attempted.
- SUBMITTING/UNKNOWN are ambiguous until an exact owned native MARKET SL
  at the proposed tighter price is observed, with all open positions
  protected, full quantity, no foreign inventory and complete order counts.
- A still-active old stop can be observed as protective but does not
  resolve an unknown amendment or authorize another POST.
- Any gap, unexpected position, insufficient coverage, errorCode, unknown
  stopLossType, stale or drifting inventory freezes all new mutations.
- Replaced stop identities additionally require positive, unique finished
  cancellation provenance for the previous native stop.
- Even a fully matched observation is NOT proof of exchange atomicity,
  stop execution through a gap, or authorization to switch live.

The upstream API adapter and external live gates remain deliberately disabled.
The separate StopUpdatePersistencePipeline may later commit a verified outcome
only after independent post-mutation GET authority + checkpoint conditions.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any, Protocol

from app.t100_live_durable_recovery import (
    PositionCheckpoint, checkpoint_from_row,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    LedgerSnapshot, _ledger_digest, _venue_digest, _valid_counts,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_expected_stop_transition import (
    ExpectedStopTransition, assess_expected_stop_current_outcome,
)
from app.t100_live_stop_journal import StopAmendment, StopJournal


class V40StopRecoveryHalted(RuntimeError):
    """No raw MEXC, credentials or SQL text in error descriptions."""


class StrictLedgerReader(Protocol):
    async def read_snapshot(self) -> LedgerSnapshot: ...


class StrictVenueReader(Protocol):
    async def open_positions(self) -> list[dict[str,Any]]: ...
    async def open_stop_orders(self) -> list[dict[str,Any]]: ...


class StrictCompletenessReader(Protocol):
    async def inspect(self) -> dict[str,Any]: ...


class FinishedStopReader(Protocol):
    async def finished_for_stop(
        self, *, symbol: str, stop_order_id: str,
    ) -> list[dict[str,Any]]: ...


@dataclass(frozen=True)
class V40StopRecoveryObservation:
    status: str
    journal_state: str
    symbol: str
    position_id: str
    active_native_market_stops: int = 0
    owned_positions: int = 0
    target_stop_order_id: str | None = None
    outcome_price_tightened: bool = False
    all_owned_native_stops_observed: bool = False
    ledger_bookends_stable: bool = False
    venue_bookends_stable: bool = False
    account_wide_completeness_observed: bool = False
    signed_get_provenance_independently_verified: bool = False
    exchange_stop_atomicity_verified: bool = False
    stop_execution_on_gap_verified: bool = False
    post_outcome_persistence_allowed: bool = False
    live_stop_mutation_allowed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _nonzero(row: dict[str,Any], key: str) -> D:
    try:
        value=D(str(row[key]))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        raise V40StopRecoveryHalted("V40_RECOVERY_POSITION_QUANTITY_INVALID") from None
    if not value.is_finite() or value<=0:
        raise V40StopRecoveryHalted("V40_RECOVERY_POSITION_QUANTITY_INVALID")
    return value


def _exact_equal_amendment(row: dict[str,Any], a: StopAmendment, state: str) -> bool:
    if row.get("external_id")!=a.external_id or row.get("state")!=state:
        return False
    if any(row.get(k)!=getattr(a,k) for k in (
        "symbol","position_id","stop_order_id",
    )):
        return False
    try:
        return (
            D(str(row.get("previous_stop")))==a.previous_stop
            and D(str(row.get("proposed_stop")))==a.proposed_stop
        )
    except (ValueError, InvalidOperation, TypeError):
        return False


class V40NativeStopRecoveryAuditor:
    """Check durable and current venue state, never promote journal states."""

    def __init__(
        self, *,
        ledger: StrictLedgerReader,
        venue: StrictVenueReader,
        completeness: StrictCompletenessReader,
        journal: StopJournal,
        finished_stops: FinishedStopReader | None = None,
    ) -> None:
        if any(x is None for x in (ledger, venue, completeness, journal)):
            raise ValueError("V40_STOP_RECOVERY_READERS_REQUIRED")
        self.ledger=ledger
        self.venue=venue
        self.completeness=completeness
        self.journal=journal
        self.finished_stops=finished_stops

    async def observe(self, amendment: StopAmendment) -> V40StopRecoveryObservation:
        if (
            type(amendment) is not StopAmendment
            or not re.fullmatch(r"[A-Z0-9]+_USDT",amendment.symbol)
            or amendment.symbol in ("BTC_USDT","ETH_USDT")
            or not amendment.position_id.isdigit()
            or not amendment.stop_order_id.isdigit()
            or amendment.proposed_stop>=amendment.previous_stop
        ):
            raise V40StopRecoveryHalted("V40_STOP_RECOVERY_AMENDMENT_INVALID")

        try:
            state=await self.journal.state(amendment.external_id)
        except Exception:
            raise V40StopRecoveryHalted("V40_STOP_RECOVERY_JOURNAL_READ_FAILED") from None
        if state=="PREPARED":
            return V40StopRecoveryObservation(
                "V40_PREPARED_WITHOUT_OWNED_MUTATION_RECONCILE_ONLY",
                state,amendment.symbol,amendment.position_id,
            )
        if state not in ("SUBMITTING","UNKNOWN","VERIFIED"):
            raise V40StopRecoveryHalted("V40_STOP_RECOVERY_JOURNAL_STATE_INVALID")

        def observed(status: str,*,count: int=0,
                     stop_id: str|None=None,tightened: bool=False,
                     bookends: bool=False,complete: bool=False,
                     owns_all: bool=False) -> V40StopRecoveryObservation:
            return V40StopRecoveryObservation(
                status,state,amendment.symbol,amendment.position_id,
                active_native_market_stops=count,
                owned_positions=count,
                target_stop_order_id=stop_id,
                outcome_price_tightened=tightened,
                ledger_bookends_stable=bookends,
                venue_bookends_stable=bookends,
                account_wide_completeness_observed=complete,
                all_owned_native_stops_observed=owns_all,
            )

        try:
            before_ledger=await self.ledger.read_snapshot()
            before_hash=_ledger_digest(before_ledger)
            before_positions=await self.venue.open_positions()
            before_stops=await self.venue.open_stop_orders()
            before_venue_hash=_venue_digest(before_positions,before_stops)
            completeness=await self.completeness.inspect()
            after_positions=await self.venue.open_positions()
            after_stops=await self.venue.open_stop_orders()
            after_venue_hash=_venue_digest(after_positions,after_stops)
            after_ledger=await self.ledger.read_snapshot()
            after_hash=_ledger_digest(after_ledger)
        except Exception:
            return observed("V40_STOP_RECOVERY_READ_OR_PROVENANCE_UNVERIFIED_FREEZE")

        if (
            before_hash!=after_hash
            or before_venue_hash!=after_venue_hash
        ):
            return observed("V40_STOP_RECOVERY_LEDGER_OR_VENUE_DRIFT_FREEZE")
        if (
            not isinstance(after_positions,list)
            or not isinstance(after_stops,list)
            or any(type(x) is not dict for x in after_positions+after_stops)
            or not _valid_counts(completeness,len(after_positions),len(after_stops))
        ):
            return observed("V40_STOP_RECOVERY_INVENTORY_INCOMPLETE_FREEZE")

        try:
            checkpoints=[
                checkpoint_from_row(row)
                for row in before_ledger.checkpoints
            ]
        except Exception:
            return observed("V40_STOP_RECOVERY_DURABLE_CHECKPOINT_INVALID_FREEZE")
        if (
            not 1<=len(checkpoints)<=4
            or len(after_positions)!=len(checkpoints)
            or len(after_stops)!=len(checkpoints)
        ):
            return observed("V40_STOP_RECOVERY_ACCOUNT_SLOT_COUNT_MISMATCH_FREEZE")

        matched_rows=[
            x for x in before_ledger.stop_amendments
            if isinstance(x,dict) and x.get("external_id")==amendment.external_id
        ]
        if len(matched_rows)!=1 or not _exact_equal_amendment(
            matched_rows[0],amendment,state,
        ):
            return observed("V40_STOP_RECOVERY_DURABLE_AMENDMENT_MISMATCH_FREEZE")
        # Do not clear one amendment while a second unresolved action
        # could independently replace/cancel the same or another stop.
        # In particular, a later exchange GET is not proof that two
        # competing SUBMITTING requests have been resolved.
        unresolved_elsewhere=[
            row for row in before_ledger.stop_amendments
            if type(row) is dict
            and row.get("external_id")!=amendment.external_id
            and row.get("state") in ("SUBMITTING","UNKNOWN")
        ]
        if unresolved_elsewhere:
            return observed("V40_STOP_RECOVERY_COMPETING_UNKNOWN_MUTATION_FREEZE")

        by_positions={}
        by_stops={}
        for row in after_positions:
            pid=str(row.get("positionId") or "")
            if not pid or pid in by_positions:
                return observed("V40_STOP_RECOVERY_POSITION_ORIGIN_AMBIGUOUS_FREEZE")
            by_positions[pid]=row
        for row in after_stops:
            pid=str(row.get("positionId") or "")
            if not pid or pid in by_stops:
                return observed("V40_STOP_RECOVERY_STOP_ORIGIN_AMBIGUOUS_FREEZE")
            by_stops[pid]=row

        owner_ids={cp.position_id for cp in checkpoints}
        if (
            len(owner_ids)!=len(checkpoints)
            or len({cp.symbol for cp in checkpoints})!=len(checkpoints)
            or set(by_positions)!=owner_ids
            or set(by_stops)!=owner_ids
        ):
            return observed("V40_STOP_RECOVERY_UNOWNED_OR_UNPROTECTED_SLOT_FREEZE")

        targets=[cp for cp in checkpoints if cp.position_id==amendment.position_id]
        if len(targets)!=1:
            return observed("V40_STOP_RECOVERY_TARGET_CHECKPOINT_MISSING_FREEZE")
        target=targets[0]
        if (
            target.symbol!=amendment.symbol
            or target.stop_order_id!=amendment.stop_order_id
            or target.stop_price!=amendment.previous_stop
            or target.lifecycle_state!="OPEN"
        ):
            return observed("V40_STOP_RECOVERY_STALE_CHECKPOINT_ORIGIN_FREEZE")

        for cp in checkpoints:
            pos=by_positions[cp.position_id]
            stop=by_stops[cp.position_id]
            try:
                qty=_nonzero(pos,"holdVol")
            except V40StopRecoveryHalted:
                return observed("V40_STOP_RECOVERY_POSITION_QUANTITY_INVALID_FREEZE")
            if not (
                cp.lifecycle_state=="OPEN"
                and qty==cp.contracts
                and pos.get("symbol")==cp.symbol
                and type(pos.get("positionType")) is int
                and pos["positionType"]==2
                and type(pos.get("state")) is int
                and pos["state"]==1
                and type(pos.get("openType")) is int
                and pos["openType"]==2
                and type(pos.get("leverage")) is int
                and pos["leverage"]==1
            ):
                return observed("V40_STOP_RECOVERY_POSITION_MODE_OR_QUANTITY_DRIFT_FREEZE")
            report=assess_native_short_market_stop_row(
                stop=stop,symbol=cp.symbol,
                position_id=cp.position_id,position_quantity=cp.contracts,
                maximum_short_stop_price=cp.stop_price,
            )
            if report.active_native_full_volume_market_sl_observed is not True:
                return observed("V40_STOP_RECOVERY_NATIVE_MARKET_FULL_VOLUME_UNVERIFIED_FREEZE")

        # The exact old journal identity (not merely a tightened price)
        # must match the current or verifiably canceled prior native stop.
        expected=ExpectedStopTransition.build(
            external_id=amendment.external_id,
            symbol=amendment.symbol,position_id=amendment.position_id,
            previous_stop_order_id=amendment.stop_order_id,
            previous_stop=amendment.previous_stop,
            proposed_stop=amendment.proposed_stop,
        )
        current_stop=by_stops[amendment.position_id]
        current_id=str(current_stop.get("id") or "")
        if state=="VERIFIED" and (
            str(matched_rows[0].get("verified_stop_order_id") or "")!=current_id
        ):
            return observed("V40_STOP_RECOVERY_VERIFIED_ID_DRIFT_FREEZE")
        historical=None
        if current_id!=amendment.stop_order_id:
            if self.finished_stops is None:
                return observed(
                    "V40_STOP_REPLACEMENT_HISTORY_REQUIRED_FREEZE",
                    count=len(checkpoints),stop_id=current_id,
                    bookends=True,complete=True,owns_all=True,
                )
            try:
                historical=await self.finished_stops.finished_for_stop(
                    symbol=amendment.symbol,
                    stop_order_id=amendment.stop_order_id,
                )
            except Exception:
                return observed(
                    "V40_STOP_REPLACEMENT_HISTORY_UNAVAILABLE_FREEZE",
                    count=len(checkpoints),stop_id=current_id,
                    bookends=True,complete=True,owns_all=True,
                )
        outcome=assess_expected_stop_current_outcome(
            current_stops=after_stops,expected=expected,
            finished_stops=historical,
        )
        if outcome.get("expected_transition_verified") is not True:
            return observed(
                "V40_NATIVE_SL_ACTIVE_AMENDMENT_UNRESOLVED_FREEZE",
                count=len(checkpoints),stop_id=current_id,
                bookends=True,complete=True,owns_all=True,
            )

        # No state transition is performed here. Upstream durable amendment
        # reconciliation + checkpoint persistence still needs independent
        # authorization, a fresh account witness and source provenance.
        return observed(
            "V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING",
            count=len(checkpoints),stop_id=current_id,
            tightened=True,bookends=True,complete=True,owns_all=True,
        )
