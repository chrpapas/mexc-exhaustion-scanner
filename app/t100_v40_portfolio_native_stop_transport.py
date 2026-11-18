"""Four-slot V40 native stop tightening: POST boundary for durable stop journal.

This is a DISTINCT adapter from the 1x/one-slot pilot. Its exchange mutation
is reachable only through the independent four-slot production permit, still
hard-blocked by current NOT_CERTIFIED release controls. Import is inert.

A caller MUST persist exactly one STOP SUBMITTING CAS with
StopMutationExecutor first. This object will then:
* validate the 14% arm/0.5pp frozen trailing provenance and exact target;
* independently verify every 1..4 owned CROSS short and native MARKET SL,
  including all non-target stops, using two full-account/ledger snapshots;
* re-read the active independent guardian before and after signed venue GETs;
* make AT MOST one MEXC native stop-plan modification POST, with no TP field;
* return ONLY "HTTP attempted", never claim that protection changed.
A timeout/ambiguous response requires externally journaled UNKNOWN and
independent GET + checkpoint recovery. No automatic retry or stop relaxation.

Official API docs for this exact TP/SL plan endpoint must be independently
certified against the target subaccount; existing LIVE_NATIVE_STOP_AMENDMENT_
STATUS=UNVERIFIED_CURRENT_DOCS intentionally prevents a live POST.
"""
from __future__ import annotations

from decimal import Decimal as D
from typing import Any, Sequence

from app import t100_live_guards
from app.t100_live_authorization import (
    LiveMutationPermit, V40_PORTFOLIO_SCOPE,
    require_certified_transport_permit,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import inspect_existing_four_slots_get_only
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_v40_native_stop_amend_transport import (
    ENDPOINT,
    V40DocumentedNativeStopAmendTransport,
    V40StopAmendmentContractHalted,
    _id, _num,
)


class V40PortfolioStopPostHalted(RuntimeError):
    """Finite, nonsecret code; never propagate MEXC, DB or signer text."""


class V40FourSlotNativeStopAmendTransport(
    V40DocumentedNativeStopAmendTransport
):
    """Exact portfolio-only stop POST, compatible with StopMutationExecutor."""

    def __init__(
        self, *, client: Any,
        checkpoints: Sequence[PositionCheckpoint],
        target_position_id: str, target_signal_id: int,
        preflight: TrailingProposalReview,
        guardian_reader: Any,
        ledger: PostgresFourSlotLedgerReader,
    ) -> None:
        super().__init__(
            client=client,checkpoints=checkpoints,
            target_position_id=target_position_id,
            target_signal_id=target_signal_id,
            preflight=preflight,guardian_reader=guardian_reader,
        )
        if type(ledger) is not PostgresFourSlotLedgerReader:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_RESTRICTED_LEDGER_REQUIRED"
            )
        if len({cp.position_id for cp in self.checkpoints})!=len(self.checkpoints):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_DUPLICATE_POSITION_NO_POST"
            )
        if len({cp.symbol for cp in self.checkpoints})!=len(self.checkpoints):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_DUPLICATE_SYMBOL_NO_POST"
            )
        if len({cp.stop_order_id for cp in self.checkpoints})!=len(self.checkpoints):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_DUPLICATE_NATIVE_ID_NO_POST"
            )
        targets=[cp for cp in self.checkpoints
                 if cp.position_id==target_position_id]
        if len(targets)!=1 or targets[0].lifecycle_state!="OPEN":
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_TARGET_NOT_OWNED_NO_POST"
            )
        self.ledger=ledger
        self.target_checkpoint=targets[0]
        self._post_attempted=False

    async def _snapshot(self) -> FourSlotSnapshotResult:
        return await inspect_existing_four_slots_get_only(
            ledger=self.ledger,client=self.client,
        )

    def _require_snapshot(self,s:object) -> tuple[str,str]:
        expected={cp.position_id:cp for cp in self.checkpoints}
        if (
            type(s) is not FourSlotSnapshotResult
            or s.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
            or s.occupied_slots!=len(expected)
            or s.entry_allowed is not False
            or s.live_authorization is not False
            or type(s.ledger_hash) is not str or len(s.ledger_hash)!=64
            or type(s.critical_inventory_hash) is not str
            or len(s.critical_inventory_hash)!=64
            or s.recovery is None
            or len(s.recovery.recovered)!=len(expected)
        ):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_WHOLE_ACCOUNT_UNVERIFIED_NO_POST"
            )
        observed={}
        for p in s.recovery.recovered:
            cp=expected.get(p.position_id)
            if (
                cp is None
                or p.position_id in observed
                or p.symbol!=cp.symbol
                or p.contracts!=cp.contracts
                or p.stop_order_id!=cp.stop_order_id
                or p.stop_price!=cp.stop_price
                or p.entry_price!=cp.entry_price
                or p.armed!=cp.armed
                or p.best_profit_pct!=cp.best_profit_pct
            ):
                raise V40PortfolioStopPostHalted(
                    "V40_PORTFOLIO_STOP_OWNED_RESTART_DRIFT_NO_POST"
                )
            observed[p.position_id]=True
        if len(observed)!=len(expected):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_OWNED_SET_INCOMPLETE_NO_POST"
            )
        return s.ledger_hash,s.critical_inventory_hash

    async def submit_native_short_stop_tightening(
        self, *, stop_order_id:int,
        new_stop_price:str, previous_stop_price:str,
        permit:LiveMutationPermit,
    ) -> None:
        # MUST be rejected before network/ledger I/O unless BOTH the
        # native-stop contract and full four-slot release are certified.
        if t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS!="CERTIFIED":
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_NATIVE_AMENDMENT_NOT_CERTIFIED_NO_POST"
            )
        try:
            checked=require_certified_transport_permit(permit)
        except Exception:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_PERMIT_NOT_CERTIFIED_NO_POST"
            ) from None
        cp=self.target_checkpoint
        p=self.preflight
        proposed=p.proposal
        old=_num(previous_stop_price)
        new=_num(new_stop_price)
        if (
            checked.scope!=V40_PORTFOLIO_SCOPE
            or checked.symbol!=cp.symbol
            or checked.signal_id!=self.target_signal_id
            or type(stop_order_id) is not int or stop_order_id<=0
            or str(stop_order_id)!=cp.stop_order_id
            or _id(stop_order_id)!=cp.stop_order_id
            or old is None or new is None or old!=cp.stop_price
            or new>=old
            or p.status!="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
            or p.account_bookends_equal is not True
            or p.all_owned_full_volume_market_stops_observed is not True
            or p.frozen_trailing_policy_matched is not True
            or p.current_stop_above_last_price is not True
            or p.market_low_provenance_verified is not True
            or p.signed_get_provenance_independently_verified is not True
            or p.exchange_atomicity_verified is not False
            or p.live_stop_mutation_allowed is not False
            or p.live_authorization is not False
            or proposed is None
            or proposed.position_id!=cp.position_id
            or proposed.symbol!=cp.symbol
            or proposed.stop_order_id!=cp.stop_order_id
            or proposed.old_stop!=old
            or proposed.proposed_stop!=new
            or proposed.armed is not True
            or proposed.best_profit_pct< D("14")
            or proposed.best_profit_pct<cp.best_profit_pct
            or cp.armed is not True
        ):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_FROZEN_TRAILING_OR_IDENTITY_DRIFT_NO_POST"
            )
        if self._post_attempted:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_PREVIOUS_POST_MAY_HAVE_OCCURRED_NO_RETRY"
            )

        # All reads use the SAME authenticated MEXC account signer as POST.
        # Snapshot includes ledger intents, immutable revisions, pending
        # amendment states and complete account-native SL inventory.
        try:
            await self._require_fresh_guardian()
            first=await self._snapshot()
            first_hashes=self._require_snapshot(first)
            probe=ReadOnlyMexcProbe(self.client)
            a=await self._frame(probe)
            b=await self._frame(probe)
            second=await self._snapshot()
            second_hashes=self._require_snapshot(second)
            await self._require_fresh_guardian()
        except Exception:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_CURRENT_SIGNED_ACCOUNT_LEDGER_OR_GUARDIAN_FAILED"
            ) from None
        if (
            first_hashes!=second_hashes or a!=b
            or a!=first_hashes[1] or a!=second_hashes[1]
        ):
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_PRE_POST_ACCOUNT_BOOKEND_DRIFT_NO_POST"
            )
        if self._post_attempted:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_PREVIOUS_POST_MAY_HAVE_OCCURRED_NO_RETRY"
            )
        # StopMutationExecutor must have durably committed SUBMITTING.
        # No TP or zero-price parameter, which might remove native cover.
        self._post_attempted=True
        try:
            await self.client._private_post(ENDPOINT,{
                "stopPlanOrderId":stop_order_id,
                "stopLossPrice":format(new,"f"),
                "lossTrend":1,
                "stopLossReverse":2,
            })
        except BaseException:
            raise V40PortfolioStopPostHalted(
                "V40_PORTFOLIO_STOP_POST_OUTCOME_UNKNOWN_GET_RECONCILE_NO_RETRY"
            ) from None
        # No ACK claims: durable journal remains SUBMITTING until fresh
        # signed native-stop GET and immutable checkpoint commit.
