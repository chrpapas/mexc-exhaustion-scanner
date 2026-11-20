"""Contract-accurate, release-gated MEXC native stop tightening transport.

Current MEXC futures docs, "Modify TP/SL Prices on a TP/SL Planned Order":
POST /api/v1/private/stoporder/change_plan_price
with stopPlanOrderId and positive stopLossPrice, optional lossTrend 1 and
stopLossReverse 2. The similar stoporder/change_price is for a LIMIT ORDER,
not the position's native planned SL.

This transport is a dependency for the durable StopMutationExecutor only:
  1. Caller has independently verified full post-fill history and prepared
     the exact StopAmendment journal.
  2. Durable executor persists SUBMITTING before this method is invoked.
  3. We re-read the signed exchange account and every native market SL.
  4. Exactly one POST attempt is made; return is NEVER final stop proof.
  5. Caller uses independent GET + durable completion handoff. Any error is
     UNKNOWN / reconciliation-only, not permission for a second attempt.

The currently unreleased global contract gate remains hard closed.
No credential loader, CLI, Render startup hook or background work here.
"""
from __future__ import annotations

from decimal import Decimal as D, InvalidOperation
from typing import Any, Protocol, Sequence

from app import t100_live_guards
from app.t100_live_authorization import (
    LiveMutationPermit, V40_CANARY_SCOPE, require_certified_transport_permit,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_snapshot_coordinator import _venue_digest
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    V40NonflatGuardianEvidence,
)

ENDPOINT="/api/v1/private/stoporder/change_plan_price"
GOOD_GUARDIAN="V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING"


class V40StopAmendmentContractHalted(RuntimeError):
    """Finite nonsecret reason; never include HTTP, account or SQL data."""


class SignedStopClient(Protocol):
    async def _private_get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> Any: ...
    async def _private_post(
        self, path: str, body: dict[str, Any],
    ) -> Any: ...


def _num(value: Any) -> D | None:
    if type(value) is bool or value is None:
        return None
    try:
        n=D(str(value))
    except (ValueError, InvalidOperation, TypeError):
        return None
    return n if n.is_finite() and n>0 else None


def _id(value: Any) -> str | None:
    if type(value) not in (int,str):
        return None
    s=str(value)
    return s if s.isascii() and s.isdecimal() and s[0]!="0" else None


def _verified_full_account(
    *, positions: Any, stops: Any, counts: Any, mode: Any,
    cert: Any, owned: tuple[PositionCheckpoint,...],
) -> str:
    """Return account digest only after every exact owned short has native SL."""
    fail=lambda code:(_ for _ in ()).throw(V40StopAmendmentContractHalted(code))
    if (
        type(positions) is not list or type(stops) is not list
        or type(counts) is not dict or type(cert) is not dict
        or type(mode) is not int or mode!=1
        or len(positions)!=len(owned) or len(stops)!=len(owned)
        or not 1<=len(owned)<=4
        or any(type(x) is not dict for x in positions+stops)
    ):
        fail("V40_STOP_AMENDMENT_ACCOUNT_SHAPE_OR_HEDGE_INVALID")
    keys=("sumCount","stopOrderCount","limitOrderCount","planOrderCount","trackOrderCount")
    if (
        set(counts)!=set(keys)
        or any(type(counts.get(k)) is not int or counts[k]<0 for k in keys)
        or counts["sumCount"]!=len(owned)
        or counts["stopOrderCount"]!=len(owned)
        or any(counts[k]!=0 for k in ("limitOrderCount","planOrderCount","trackOrderCount"))
    ):
        fail("V40_STOP_AMENDMENT_ACCOUNT_ORDER_INVENTORY_UNVERIFIED")
    ids={cp.position_id for cp in owned}
    if (
        len(ids)!=len(owned)
        or len({cp.symbol for cp in owned})!=len(owned)
        or len({cp.stop_order_id for cp in owned})!=len(owned)
        or any(type(cp) is not PositionCheckpoint or cp.lifecycle_state!="OPEN"
               for cp in owned)
        or {_id(p.get("positionId")) for p in positions}!=ids
        or {_id(s.get("positionId")) for s in stops}!=ids
    ):
        fail("V40_STOP_AMENDMENT_ACCOUNT_OWNERSHIP_INVALID")
    by_pos={_id(p["positionId"]):p for p in positions}
    by_stop={_id(s["positionId"]):s for s in stops}
    for cp in owned:
        p,s=by_pos[cp.position_id],by_stop[cp.position_id]
        if (
            cp.symbol!=p.get("symbol") or cp.symbol!=s.get("symbol")
            or p.get("positionType")!=2
            or p.get("openType")!=2 or p.get("state")!=1
            or type(p.get("leverage")) is not int
            or not 1<=p["leverage"]<=125
            or _num(p.get("holdVol"))!=cp.contracts
            or _id(s.get("id") if s.get("id") is not None
                   else s.get("stopPlanOrderId"))!=cp.stop_order_id
            or _num(s.get("stopLossPrice"))!=cp.stop_price
        ):
            fail("V40_STOP_AMENDMENT_OPEN_POSITION_OR_STOP_CHANGED")
        audit=assess_native_short_market_stop_row(
            stop=s,symbol=cp.symbol,position_id=cp.position_id,
            position_quantity=cp.contracts,
            maximum_short_stop_price=cp.stop_price,
            verified_filled_entry_order_id=cp.exchange_order_id,
        )
        if audit.active_native_full_volume_market_sl_observed is not True:
            fail("V40_STOP_AMENDMENT_NATIVE_MARKET_FULL_COVERAGE_UNVERIFIED")
    try:
        digest=_venue_digest(positions,stops)
    except Exception:
        fail("V40_STOP_AMENDMENT_ACCOUNT_DIGEST_INVALID")
    if (
        cert.get("account_wide_completeness_verified") is not True
        or cert.get("critical_inventory_equal_across_witness_window") is not True
        or cert.get("critical_inventory_sha256")!=digest
        or cert.get("exchange_atomicity_verified") is not False
        or cert.get("entry_allowed") is not False
        or cert.get("live_authorization") is not False
        or cert.get("before_positions")!=len(owned)
        or cert.get("after_positions")!=len(owned)
        or cert.get("before_current_tpsl")!=len(owned)
        or cert.get("after_current_tpsl")!=len(owned)
    ):
        fail("V40_STOP_AMENDMENT_INVENTORY_CERTIFICATE_UNVERIFIED")
    return digest


class V40DocumentedNativeStopAmendTransport:
    """Conservative STOP PLAN POST implementation; disabled until reviewed.

    Only the one-position signal-bound canary permit exists today. The
    historical four-slot release is NOT authorized by that permit. A future
    portfolio permit needs its own independent issuance and review.
    """

    def __init__(
        self, *, client: SignedStopClient,
        checkpoints: Sequence[PositionCheckpoint],
        target_position_id: str, target_signal_id: int,
        preflight: TrailingProposalReview,
        guardian_reader: Any,
    ) -> None:
        if (
            not callable(getattr(client,"_private_get",None))
            or not callable(getattr(client,"_private_post",None))
            or type(checkpoints) not in (tuple,list)
            or not 1<=len(checkpoints)<=4
            or any(type(cp) is not PositionCheckpoint for cp in checkpoints)
            or type(target_position_id) is not str or _id(target_position_id) is None
            or type(target_signal_id) is not int or target_signal_id<=0
            or type(preflight) is not TrailingProposalReview
            or not callable(getattr(guardian_reader,"verify",None))
        ):
            raise V40StopAmendmentContractHalted("V40_STOP_AMENDMENT_BINDING_INVALID")
        self.client=client
        self.checkpoints=tuple(checkpoints)
        self.target_position_id=target_position_id
        self.target_signal_id=target_signal_id
        self.preflight=preflight
        self.guardian_reader=guardian_reader

    async def _require_fresh_guardian(self) -> None:
        try:
            g=await self.guardian_reader.verify()
        except Exception:
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_GUARDIAN_DATABASE_UNAVAILABLE"
            ) from None
        if (
            type(g) is not V40NonflatGuardianEvidence
            or g.status!=GOOD_GUARDIAN
            or g.guardian_protected_observation_fresh is not True
            or g.no_current_scope_critical_or_pending_incidents is not True
            or g.live_authorization is not False
            or g.next_entry_allowed is not False
            or g.exchange_posts!=0 or g.database_writes!=0
        ):
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_GUARDIAN_LATCH_OR_FRESHNESS_HALT"
            )

    async def _frame(self, probe: ReadOnlyMexcProbe) -> str:
        positions=await probe.raw_open_positions()
        stops=await probe.raw_open_stops()
        counts=await probe.raw_open_order_counts()
        mode=await probe.raw_position_mode()
        cert=await run_current_inventory_completeness_probe(probe)
        return _verified_full_account(
            positions=positions,stops=stops,counts=counts,mode=mode,
            cert=cert,owned=self.checkpoints,
        )

    async def submit_native_short_stop_tightening(
        self, *, stop_order_id: int,
        new_stop_price: str, previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None:
        # No live request, DB change or signed GET before BOTH independent
        # transport gates pass. An env flag cannot turn this into a permit.
        if t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS!="CERTIFIED":
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_CURRENT_EXCHANGE_LIFECYCLE_NOT_CERTIFIED"
            )
        try:
            checked=require_certified_transport_permit(permit)
        except Exception:
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_TYPED_PERMIT_UNAVAILABLE"
            ) from None
        if (
            checked.scope!=V40_CANARY_SCOPE
            or len(self.checkpoints)!=1
            or checked.signal_id!=self.target_signal_id
            or checked.symbol!=self.checkpoints[0].symbol
            or self.checkpoints[0].position_id!=self.target_position_id
        ):
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_CANARY_SCOPE_OR_IDENTITY_INVALID"
            )
        cp=self.checkpoints[0]
        old=_num(previous_stop_price)
        new=_num(new_stop_price)
        p=self.preflight
        proposal=p.proposal

        if (
            type(stop_order_id) is not int
            or stop_order_id<=0 or str(stop_order_id)!=cp.stop_order_id
            or old is None or new is None or old!=cp.stop_price
            or new>=old
            or p.status!="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
            or p.account_bookends_equal is not True
            or p.all_owned_full_volume_market_stops_observed is not True
            or p.frozen_trailing_policy_matched is not True
            or p.current_stop_above_last_price is not True
            or p.market_low_provenance_verified is not True
            or p.signed_get_provenance_independently_verified is not True
            or proposal is None
            or proposal.symbol!=cp.symbol
            or proposal.position_id!=cp.position_id
            or proposal.stop_order_id!=cp.stop_order_id
            or proposal.old_stop!=old or proposal.proposed_stop!=new
        ):
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_CURRENT_PROVENANCE_UNVERIFIED"
            )
        # A cached guardian dataclass would be unsafe: re-read the
        # independent database/outbox scope now and again after both venue
        # snapshots, immediately before one possible POST.
        await self._require_fresh_guardian()
        probe=ReadOnlyMexcProbe(self.client)
        try:
            first=await self._frame(probe)
            second=await self._frame(probe)
        except Exception:
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_CURRENT_SIGNED_BOOKEND_UNVERIFIED"
            ) from None
        if first!=second:
            raise V40StopAmendmentContractHalted(
                "V40_NATIVE_STOP_CURRENT_BOOKEND_DRIFT"
            )
        await self._require_fresh_guardian()
        # Do NOT pass takeProfitPrice=0 or an empty parameter; those values
        # may cancel a TP/SL. Reconcile the same native ID after the POST.
        body={
            "stopPlanOrderId":stop_order_id,
            "stopLossPrice":format(new,"f"),
            "lossTrend":1,
            "stopLossReverse":2,
        }
        # At most ONE call. Durable executor already marked SUBMITTING;
        # result cannot be treated as finality without independent GET.
        await self.client._private_post(ENDPOINT,body)
