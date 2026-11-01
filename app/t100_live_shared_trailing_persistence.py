"""Non-authorizing shared-account T100 trailing-stop persistence boundary.

Composes the existing durable stop-amendment coordinator and checkpoint
persistence pipeline, AFTER checking:
* a real typed, bookended pre-change full-account/ledger witness;
* T100-owned position and stop identities, not operator/manual positions;
* two matching post-change full-account snapshots plus complete order counts;
* exact frozen V40 14% activation / 0.5pp trailing calculation;
* persisted amendment journal state SUBMITTING, UNKNOWN or VERIFIED.

The two post snapshots are caller-supplied offline evidence, NOT proof of
independent live GET provenance, timing/atomicity or real exchange execution.
This bridge has no order POST or MEXC client and cannot authorize trading.
On checkpoint failure, the journal stays VERIFIED; restarting reconciles
exchange state and may retry DB checkpoint persistence, NEVER the MEXC POST.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _valid_counts, _venue_digest,
)
from app.t100_live_shared_trailing_scope import (
    observe_shared_t100_trailing_change,
)
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_stop_update_pipeline import (
    StopUpdatePersistenceOutcome, StopUpdatePersistencePipeline,
    StopUpdatePipelineHalted,
)
from app.t100_live_trailing import next_short_stop


class SharedTrailingPipelineHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class SharedTrailingPersistenceOutcome:
    checkpoint: PositionCheckpoint
    persistence: StopUpdatePersistenceOutcome
    status: str = "SHARED_T100_TRAILING_CHECKPOINT_WRITTEN_NONAUTHORIZING"
    operator_positions_mutated_by_coordinator: bool = False
    exchange_mutation_sent_by_coordinator: bool = False
    exchange_native_stop_execution_certified: bool = False
    post_bookend_get_provenance_independently_certified: bool = False
    account_cross_margin_certified: bool = False
    live_authorization: bool = False
    entry_allowed: bool = False


class SharedTrailingPersistencePipeline:
    def __init__(self, inner: StopUpdatePersistencePipeline) -> None:
        if type(inner) is not StopUpdatePersistencePipeline:
            raise ValueError("SHARED_TRAILING_PIPELINE_MISSING")
        self.inner = inner

    async def reconcile_bookended_and_persist(
        self, *,
        pre_witness: FourSlotSnapshotResult,
        checkpoint: PositionCheckpoint,
        amendment: StopAmendment,
        before_positions: Any,
        before_stops: Any,
        after_positions_first: Any,
        after_stops_first: Any,
        after_inventory_first: Any,
        after_positions_second: Any,
        after_stops_second: Any,
        after_inventory_second: Any,
        observed_low: Decimal,
        price_tick: Decimal,
        finished_stops: Any | None = None,
        restart_reconciliation: bool = False,
    ) -> SharedTrailingPersistenceOutcome:
        """Pure evidence checks first; only then possible journal+DB writes.

        restart_reconciliation=True uses existing reconciliation-only recovery
        from SUBMITTING/UNKNOWN/VERIFIED. Never re-attempt MEXC mutation.
        """
        if (
            type(restart_reconciliation) is not bool
            or type(checkpoint) is not PositionCheckpoint
            or type(amendment) is not StopAmendment
            or type(observed_low) is not Decimal
            or type(price_tick) is not Decimal
            or type(after_positions_first) is not list
            or type(after_stops_first) is not list
            or type(after_positions_second) is not list
            or type(after_stops_second) is not list
        ):
            raise SharedTrailingPipelineHalted("SHARED_TRAILING_INPUT_UNVERIFIED")

        try:
            # Compare BOTH independent complete post snapshots, not a single
            # passed-in boolean. Economic and timing freshness are NOT proven.
            if (
                not _valid_counts(
                    after_inventory_first,
                    len(after_positions_first), len(after_stops_first),
                )
                or not _valid_counts(
                    after_inventory_second,
                    len(after_positions_second), len(after_stops_second),
                )
                or _venue_digest(
                    after_positions_first, after_stops_first,
                ) != _venue_digest(
                    after_positions_second, after_stops_second,
                )
            ):
                raise SharedTrailingPipelineHalted(
                    "SHARED_TRAILING_POST_BOOKENDS_UNVERIFIED"
                )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_POST_BOOKENDS_UNVERIFIED"
            ) from None

        try:
            step = next_short_stop(
                entry=checkpoint.entry_price,
                low=observed_low,
                current_stop=checkpoint.stop_price,
                best_profit_pct=checkpoint.best_profit_pct,
                price_tick=price_tick,
                armed=checkpoint.armed,
            )
        except (ValueError, TypeError, ArithmeticError):
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_FROZEN_POLICY_INPUT_INVALID"
            ) from None
        if (
            not step.active or not step.changed
            or step.stop_price != amendment.proposed_stop
            or amendment.previous_stop != checkpoint.stop_price
            or amendment.symbol != checkpoint.symbol
            or amendment.position_id != checkpoint.position_id
            or amendment.stop_order_id != checkpoint.stop_order_id
        ):
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_FROZEN_POLICY_OR_IDENTITY_MISMATCH"
            )

        try:
            # The actual durable state, not a caller-supplied flag, decides
            # whether an observed transition can be T100-owned.
            state = await self.inner.amendments.state(amendment)
        except Exception:
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_DURABLE_AMENDMENT_UNAVAILABLE"
            ) from None
        if state not in {"SUBMITTING", "UNKNOWN", "VERIFIED"}:
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_AMENDMENT_NOT_SUBMITTED"
            )

        scope = observe_shared_t100_trailing_change(
            pre_witness=pre_witness,
            checkpoint=checkpoint,
            amendment=amendment,
            amendment_journal_state=state,
            before_positions=before_positions,
            before_stops=before_stops,
            after_positions=after_positions_second,
            after_stops=after_stops_second,
            after_inventory=after_inventory_second,
            finished_stops=finished_stops,
        )
        if (
            scope.status !=
                "SHARED_TRAILING_T100_OWNED_STOP_OBSERVED_NONAUTHORIZING"
            or scope.owned_position_identity_matched is not True
            or scope.manual_and_other_owned_inventory_unchanged is not True
            or scope.native_stop_full_volume_observed is not True
            or scope.expected_tightening_observed is not True
            or scope.live_authorization is not False
        ):
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_ACCOUNT_OR_NATIVE_STOP_UNVERIFIED:"
                + scope.status
            )

        # The journal remains VERIFIED if checkpoint persistence fails.
        # Thus a subsequent process can resume without any order mutation.
        try:
            if restart_reconciliation:
                result = await self.inner.reconcile_current_and_persist(
                    checkpoint, amendment,
                    current_stops=after_stops_second,
                    finished_stops=finished_stops,
                    best_profit_pct=step.best_profit_pct,
                    armed=step.active,
                    evidence_fresh=True,
                    account_reconciled=True,
                    positions_complete=True,
                    stops_complete=True,
                )
            else:
                result = await self.inner.reconcile_observed_and_persist(
                    checkpoint, amendment,
                    before_stops=before_stops,
                    after_stops=after_stops_second,
                    finished_stops=finished_stops,
                    best_profit_pct=step.best_profit_pct,
                    armed=step.active,
                    evidence_fresh=True,
                    account_reconciled=True,
                    positions_complete=True,
                    stops_complete=True,
                )
        except StopUpdatePipelineHalted as exc:
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_CHECKPOINT_PERSISTENCE_FAILED"
            ) from exc
        except Exception:
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_JOURNAL_OR_RECONCILIATION_FAILED"
            ) from None
        if (
            result.checkpoint.position_id != checkpoint.position_id
            or result.checkpoint.symbol != checkpoint.symbol
            or result.checkpoint.checkpoint_version !=
                checkpoint.checkpoint_version + 1
            or result.checkpoint.stop_price > amendment.proposed_stop
        ):
            raise SharedTrailingPipelineHalted(
                "SHARED_TRAILING_RESULT_CHECKPOINT_MISMATCH"
            )
        return SharedTrailingPersistenceOutcome(
            checkpoint=result.checkpoint, persistence=result,
        )
