"""Guarded V40 in-place native-stop completion -> durable checkpoint journal.

This is a deliberately unregistered and OFF-by-default mutation-capable
component. It sends NO exchange orders. The sole potential mutation is the
existing durable journal/checkpoint pipeline, reachable ONLY after a typed
certified transport permit (currently unavailable in production), a
positive read-only whole-account recovery result, a complete signed-fill
trailing state and two independent signed MEXC GET bookends.

The component never grants that permit, signs a synthetic one, installs a
Render runtime, or enables the native stop POST adapter. It does not claim
exchange atomicity or position protection through a market gap.

Replacement stop identities require a separate terminal-history and
checkpoint-signature protocol. Those are intentionally blocked here.
Unknown/unchanged stops and unexpected account inventory stay unresolved.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any, Callable

from app.t100_live_authorization import (
    LiveMutationPermit,require_certified_transport_permit,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_stop_update_pipeline import StopUpdatePersistencePipeline
from app.t100_live_v40_native_stop_recovery import (
    V40NativeStopRecoveryAuditor,V40StopRecoveryObservation,
)
from app.t100_live_v40_trailing_restart import V40TrailingRestartView


class V40StopCompletionHalted(RuntimeError):
    """No raw exchange/database errors or secret identifiers."""


def _strict_bookend_fingerprint(
    positions: list[dict[str,Any]],
    stops: list[dict[str,Any]],
    counts: dict[str,int],
    position_mode: int,
) -> str:
    """Compare the ENTIRE validated signed GET inventory, not selected fields.

    The earlier positional projection omitted volume-mode, stop origin,
    stopLossVol, stopLossReverse, lossTrend and other protective data.
    Drift in any exchange field must freeze checkpoint persistence even
    when the two snapshots separately look safe. No raw account data
    is returned or logged. Unsupported object/NaN types fail closed.
    """
    def canonical(value: Any) -> Any:
        if value is None or type(value) in (bool,int,str):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("NONFINITE_SIGNED_GET_VALUE")
            return value
        if type(value) is D:
            if not value.is_finite():
                raise ValueError("NONFINITE_SIGNED_GET_DECIMAL")
            return {"decimal":str(value)}
        if type(value) is list:
            return [canonical(item) for item in value]
        if type(value) is dict:
            if any(type(k) is not str for k in value):
                raise ValueError("SIGNED_GET_NONSTRING_KEY")
            return {k:canonical(v) for k,v in value.items()}
        raise ValueError("SIGNED_GET_NONCANONICAL_VALUE")
    return json.dumps(
        canonical({
            "positions":positions,
            "stops":stops,
            "counts":counts,
            "position_mode":position_mode,
        }),
        sort_keys=True,separators=(",",":"),allow_nan=False,
    )


@dataclass(frozen=True)
class V40StopCompletionResult:
    status: str
    journal_checkpoint_committed: bool=False
    needs_post_commit_independent_restart_check: bool=True
    exchange_posts: int=0
    live_authorization: bool=False
    entry_allowed: bool=False


class V40NativeStopCompletionHandoff:
    """Never re-submit an amendment; never touch live without permit."""

    def __init__(
        self,*,auditor:V40NativeStopRecoveryAuditor,
        probe:ReadOnlyMexcProbe,
        pipeline:StopUpdatePersistencePipeline,
        clock_ms:Callable[[],int]|None=None,
    ):
        if (
            type(auditor) is not V40NativeStopRecoveryAuditor
            or type(probe) is not ReadOnlyMexcProbe
            or type(pipeline) is not StopUpdatePersistencePipeline
        ):
            raise ValueError("STOP_COMPLETION_REQUIRES_TYPED_READERS_AND_JOURNAL")
        self.auditor=auditor
        self.probe=probe
        self.pipeline=pipeline
        self.clock_ms=clock_ms or (lambda:int(time.time()*1000))

    async def commit_in_place_if_verified(
        self,*,amendment:StopAmendment,
        previous:PositionCheckpoint,
        trailing:V40TrailingRestartView,
        permit:LiveMutationPermit,
    ) -> V40StopCompletionResult:
        # The live adapter is NOT_IMPLEMENTED at this release. Even a
        # perfect GET result cannot activate durable writes from shadow
        # mode. The typed permit must be independently issued first.
        require_certified_transport_permit(permit)

        if not (
            type(amendment) is StopAmendment
            and type(previous) is PositionCheckpoint
            and type(trailing) is V40TrailingRestartView
            and previous.lifecycle_state=="OPEN"
            and previous.position_id==amendment.position_id==trailing.position_id
            and previous.symbol==amendment.symbol==trailing.symbol
            and previous.stop_order_id==amendment.stop_order_id
            and previous.stop_price==amendment.previous_stop
            and trailing.checkpoint_version==previous.checkpoint_version
            and trailing.recovered_stop_price==previous.stop_price
            and trailing.recovered_best_profit_pct==previous.best_profit_pct
            and trailing.recovered_armed==previous.armed
            and trailing.durable_revision_history_verified is True
            and trailing.post_fill_low_stream_complete is True
            and trailing.status=="V40_TRAILING_RESTART_CANDIDATE_REBUILT_NONAUTHORIZING"
            and trailing.next_best_profit_pct>=previous.best_profit_pct
            and trailing.next_armed is True
            and trailing.next_best_profit_pct>=D(14)
            and trailing.proposed_stop_price<=amendment.proposed_stop
            and amendment.proposed_stop<previous.stop_price
        ):
            raise V40StopCompletionHalted(
                "STOP_COMPLETION_TRAILING_CHECKPOINT_ORIGIN_UNVERIFIED"
            )
        if trailing.live_authorization is not False or trailing.stop_amendment_authorized is not False:
            raise V40StopCompletionHalted(
                "STOP_COMPLETION_UNEXPECTED_TRAILING_AUTHORITY"
            )

        audit=await self.auditor.observe(amendment)
        if not (
            type(audit) is V40StopRecoveryObservation
            and audit.status==(
                "V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING"
            )
            and audit.journal_state in ("SUBMITTING","UNKNOWN","VERIFIED")
            and audit.position_id==previous.position_id
            and audit.symbol==previous.symbol
            and audit.target_stop_order_id==amendment.stop_order_id
            and audit.outcome_price_tightened is True
            and audit.all_owned_native_stops_observed is True
            and audit.ledger_bookends_stable is True
            and audit.venue_bookends_stable is True
            and audit.account_wide_completeness_observed is True
        ):
            return V40StopCompletionResult(
                "STOP_COMPLETION_WHOLE_ACCOUNT_RECOVERY_UNVERIFIED_FREEZE"
            )

        # The auditor has checked its ledger and two venue snapshots. Read
        # the actual signed venue again *through an independent strict
        # GET-only probe*, in two complete account bookends, and require
        # stable exact static position/native SL identity.
        snapshots=[]
        try:
            begin=self.clock_ms()
            for _ in range(2):
                positions=await self.probe.raw_open_positions()
                stops=await self.probe.raw_open_stops()
                counts=await self.probe.raw_open_order_counts()
                position_mode=await self.probe.raw_position_mode()
                if (
                    type(positions) is not list
                    or type(stops) is not list
                    or type(counts) is not dict
                    or len(positions)!=audit.owned_positions
                    or len(stops)!=audit.active_native_market_stops
                    or counts.get("stopOrderCount")!=len(stops)
                    or counts.get("sumCount")!=len(stops)
                    or any(
                        counts.get(k)!=0 for k in (
                            "limitOrderCount","planOrderCount","trackOrderCount",
                        )
                    )
                    or position_mode!=1
                ):
                    return V40StopCompletionResult(
                        "STOP_COMPLETION_SIGNED_INVENTORY_INCOMPLETE_FREEZE"
                    )
                # Independently validate *all* currently open native
                # MARKET stops, not just this amendment's target. The
                # earlier auditor checked checkpoint prices for the same
                # account; this signed GET rechecks current full coverage.
                pos_by_id={}
                stop_by_id={}
                for row in positions:
                    pid=str(row.get("positionId") or "")
                    if not pid or pid in pos_by_id:
                        return V40StopCompletionResult(
                            "STOP_COMPLETION_SIGNED_POSITION_ORIGIN_AMBIGUOUS_FREEZE"
                        )
                    pos_by_id[pid]=row
                for row in stops:
                    pid=str(row.get("positionId") or "")
                    if not pid or pid in stop_by_id:
                        return V40StopCompletionResult(
                            "STOP_COMPLETION_SIGNED_STOP_ORIGIN_AMBIGUOUS_FREEZE"
                        )
                    stop_by_id[pid]=row
                if set(pos_by_id)!=set(stop_by_id):
                    return V40StopCompletionResult(
                        "STOP_COMPLETION_SIGNED_UNPROTECTED_POSITION_FREEZE"
                    )
                for pid,pos in pos_by_id.items():
                    sl=stop_by_id[pid]
                    if (
                        pos.get("symbol")!=sl.get("symbol")
                        or pos.get("positionType")!=2
                        or pos.get("openType")!=2
                        or pos.get("leverage")!=1
                        or pos.get("state")!=1
                    ):
                        return V40StopCompletionResult(
                            "STOP_COMPLETION_SIGNED_POSITION_MODE_UNVERIFIED_FREEZE"
                        )
                    contracts=D(str(pos.get("holdVol")))
                    price=D(str(sl.get("stopLossPrice")))
                    if (
                        not contracts.is_finite() or contracts<=0
                        or not price.is_finite() or price<=0
                        or assess_native_short_market_stop_row(
                            stop=sl,symbol=pos["symbol"],
                            position_id=pid,
                            position_quantity=contracts,
                            maximum_short_stop_price=price,
                        ).active_native_full_volume_market_sl_observed is not True
                    ):
                        return V40StopCompletionResult(
                            "STOP_COMPLETION_SIGNED_NATIVE_FULL_COVERAGE_UNVERIFIED_FREEZE"
                        )
                target=[
                    row for row in stops
                    if row.get("symbol")==previous.symbol
                    and str(row.get("positionId") or "")==previous.position_id
                ]
                own=[
                    row for row in positions
                    if row.get("symbol")==previous.symbol
                    and str(row.get("positionId") or "")==previous.position_id
                ]
                if len(target)!=1 or len(own)!=1 or (
                    str(target[0].get("id") or "")!=amendment.stop_order_id
                ):
                    return V40StopCompletionResult(
                        "STOP_COMPLETION_NATIVE_ID_OR_POSITION_DRIFT_FREEZE"
                    )
                row=target[0]
                qty=own[0].get("holdVol")
                if (
                    D(str(qty))!=previous.contracts
                    or own[0].get("positionType")!=2
                    or own[0].get("openType")!=2
                    or own[0].get("leverage")!=1
                    or own[0].get("state")!=1
                    or assess_native_short_market_stop_row(
                        stop=row,symbol=previous.symbol,
                        position_id=previous.position_id,
                        position_quantity=previous.contracts,
                        maximum_short_stop_price=amendment.proposed_stop,
                    ).active_native_full_volume_market_sl_observed is not True
                ):
                    return V40StopCompletionResult(
                        "STOP_COMPLETION_SIGNED_NATIVE_FULL_COVERAGE_UNVERIFIED_FREEZE"
                    )
                snapshots.append(_strict_bookend_fingerprint(
                    positions,stops,counts,position_mode,
                ))
            end=self.clock_ms()
        except Exception:
            return V40StopCompletionResult(
                "STOP_COMPLETION_SIGNED_GET_FAILED_FREEZE"
            )
        if (
            type(begin) is not int or type(end) is not int
            or end<begin or end-begin>1500
            or snapshots[0]!=snapshots[1]
        ):
            return V40StopCompletionResult(
                "STOP_COMPLETION_BOOKEND_STALE_OR_DRIFT_FREEZE"
            )

        # The pipeline owns the journal CAS and checkpoint version CAS.
        # A journal VERIFIED + failed checkpoint write remains a durable
        # recovery blocker. Never send another stop POST to repair it.
        try:
            outcome=await self.pipeline.reconcile_current_and_persist(
                previous,amendment,
                current_stops=stops,
                best_profit_pct=trailing.next_best_profit_pct,
                armed=trailing.next_armed,
                evidence_fresh=True,account_reconciled=True,
                positions_complete=True,stops_complete=True,
            )
        except Exception:
            return V40StopCompletionResult(
                "STOP_COMPLETION_DURABLE_JOURNAL_OR_CHECKPOINT_UNVERIFIED_FREEZE"
            )
        if (
            outcome.checkpoint.checkpoint_version
            !=previous.checkpoint_version+1
            or outcome.checkpoint.stop_price>amendment.proposed_stop
        ):
            return V40StopCompletionResult(
                "STOP_COMPLETION_COMMITTED_CHECKPOINT_SHAPE_UNVERIFIED_FREEZE"
            )
        return V40StopCompletionResult(
            "STOP_COMPLETION_JOURNAL_CHECKPOINT_PERSISTED_RESTART_REQUIRED",
            journal_checkpoint_committed=True,
        )
