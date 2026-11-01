"""GET-only ownership boundary for a T100 trailing-stop change in a shared account.

Given complete full-account before/after observations and the historic
bookended T100 checkpoint witness, recognize exactly ONE exchange-native
stop tightening belonging to T100. Neither manual positions nor their TP/SL
rows may change in safety-critical fields. The journal must have crossed
PREPARED -> SUBMITTING; a missing or uncertain result must NEVER authorize
a repeated POST.

This is intentionally NOT connected to the stop mutation transport or
production checkpoint pipeline. It cannot prove instantaneous venue atomicity,
actual exchange stop execution, durable amendment state, wallet collateral,
or live order authorization. The after inventory is independently supplied
and must be refreshed/bookended by a future execution coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_expected_stop_transition import (
    ExpectedStopTransition, assess_expected_stop_transition,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _valid_counts, _venue_digest,
)
from app.t100_live_shared_account_reservation_scope import (
    SharedAccountScopeHalted, inspect_shared_account_scope,
    _map_positions, _map_stops,
)
from app.t100_live_stop_journal import StopAmendment

D = Decimal


@dataclass(frozen=True)
class SharedTrailingScope:
    status: str
    owned_position_identity_matched: bool = False
    manual_and_other_owned_inventory_unchanged: bool = False
    native_stop_full_volume_observed: bool = False
    expected_tightening_observed: bool = False
    account_positions_observed: int | None = None
    manual_positions_observed: int | None = None
    independent_post_bookend_verified: bool = False
    amendment_durable_state_independently_verified: bool = False
    exchange_atomicity_verified: bool = False
    live_stop_mutation_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _price(value: Any) -> D:
    if value is None or type(value) is bool:
        raise ValueError("TRAIL_PRICE_INVALID")
    try:
        price = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("TRAIL_PRICE_INVALID") from None
    if not price.is_finite() or price <= 0:
        raise ValueError("TRAIL_PRICE_INVALID")
    return price


def observe_shared_t100_trailing_change(
    *,
    pre_witness: FourSlotSnapshotResult,
    checkpoint: PositionCheckpoint,
    amendment: StopAmendment,
    amendment_journal_state: str,
    before_positions: Any,
    before_stops: Any,
    after_positions: Any,
    after_stops: Any,
    after_inventory: Any,
    finished_stops: Any | None = None,
) -> SharedTrailingScope:
    def blocked(status: str, **information: Any) -> SharedTrailingScope:
        return SharedTrailingScope(status=status, **information)

    if (
        type(pre_witness) is not FourSlotSnapshotResult
        or type(checkpoint) is not PositionCheckpoint
        or type(amendment) is not StopAmendment
        or amendment_journal_state not in {"SUBMITTING", "UNKNOWN", "VERIFIED"}
    ):
        return blocked("SHARED_TRAILING_WITNESS_OR_JOURNAL_STATE_UNVERIFIED")

    try:
        scope = inspect_shared_account_scope(
            pre_witness, before_positions, before_stops,
        )
    except (SharedAccountScopeHalted, TypeError, ValueError):
        return blocked("SHARED_TRAILING_PRE_ACCOUNT_UNVERIFIED")
    if (
        scope.owned_count == 0
        or checkpoint.position_id is None
        or not checkpoint.position_id
        or checkpoint.lifecycle_state != "OPEN"
        or type(checkpoint.checkpoint_version) is not int
        or checkpoint.checkpoint_version <= 0
        or amendment.position_id != checkpoint.position_id
        or amendment.symbol != checkpoint.symbol
        or amendment.stop_order_id != checkpoint.stop_order_id
        or amendment.previous_stop != checkpoint.stop_price
        or amendment.proposed_stop >= amendment.previous_stop
        or checkpoint.symbol not in scope.owned_symbols
    ):
        return blocked("SHARED_TRAILING_T100_OWNERSHIP_OR_STOP_UNVERIFIED")

    recovery = pre_witness.recovery
    matched = [
        row for row in recovery.recovered
        if row.position_id == checkpoint.position_id
        and row.symbol == checkpoint.symbol
    ]
    if (
        len(matched) != 1
        or matched[0].stop_order_id != checkpoint.stop_order_id
        or matched[0].contracts != checkpoint.contracts
        or matched[0].entry_price != checkpoint.entry_price
        or matched[0].stop_price != checkpoint.stop_price
        or matched[0].best_profit_pct != checkpoint.best_profit_pct
        or matched[0].armed != checkpoint.armed
    ):
        return blocked("SHARED_TRAILING_CHECKPOINT_RECOVERY_DRIFT")

    if (
        type(after_positions) is not list
        or type(after_stops) is not list
        or not _valid_counts(
            after_inventory, len(after_positions), len(after_stops),
        )
    ):
        return blocked("SHARED_TRAILING_AFTER_ACCOUNT_COMPLETENESS_UNVERIFIED")

    common = dict(
        owned_position_identity_matched=True,
        account_positions_observed=scope.position_count,
        manual_positions_observed=scope.manual_count,
    )
    try:
        pre_pos = _map_positions(before_positions)
        post_pos = _map_positions(after_positions)
        pre_stops = _map_stops(before_stops, pre_pos)
        post_stops = _map_stops(after_stops, post_pos)
        if (
            len(post_pos) != scope.position_count
            or _venue_digest(before_positions, []) !=
                _venue_digest(after_positions, [])
        ):
            return blocked("SHARED_TRAILING_POSITION_DRIFT", **common)
        pid = checkpoint.position_id
        if len(pre_stops.get(pid, [])) != 1 or len(post_stops.get(pid, [])) != 1:
            return blocked("SHARED_TRAILING_TARGET_NATIVE_STOP_NOT_UNIQUE", **common)
        # Full-account account-side evidence must match for every position
        # OTHER than the owned checkpoint target. This includes all manual
        # TP-only or SL/TP rows, even if they are on ISOLATED positions.
        untouched_before = [
            row for row in before_stops if str(row.get("positionId")) != pid
        ]
        untouched_after = [
            row for row in after_stops if str(row.get("positionId")) != pid
        ]
        if _venue_digest([], untouched_before) != _venue_digest(
            [], untouched_after
        ):
            return blocked("SHARED_TRAILING_MANUAL_OR_OTHER_STOP_DRIFT", **common)
        previous = pre_stops[pid][0]
        current = post_stops[pid][0]
        if (
            str(previous.get("id") or previous.get("stopPlanOrderId"))
                != checkpoint.stop_order_id
            or _price(previous.get("stopLossPrice")) != checkpoint.stop_price
            or effective_protective_quantity(
                previous, position_quantity=checkpoint.contracts,
            ) != checkpoint.contracts
            or _price(current.get("stopLossPrice")) > amendment.proposed_stop
            or effective_protective_quantity(
                current, position_quantity=checkpoint.contracts,
            ) != checkpoint.contracts
            or (
                current.get("positionType") is not None
                and current.get("positionType") != 2
            )
            or (
                current.get("isFinished") is not None
                and current.get("isFinished") != 0
            )
        ):
            return blocked("SHARED_TRAILING_STOP_PROTECTION_INVALID", **common)

        expected = ExpectedStopTransition.build(
            external_id=amendment.external_id,
            symbol=amendment.symbol,
            position_id=amendment.position_id,
            previous_stop_order_id=amendment.stop_order_id,
            previous_stop=amendment.previous_stop,
            proposed_stop=amendment.proposed_stop,
        )
        evidence = assess_expected_stop_transition(
            before_stops=before_stops,
            after_stops=after_stops,
            finished_stops=finished_stops,
            expected=expected,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return blocked("SHARED_TRAILING_EXCHANGE_FIELDS_UNVERIFIED", **common)
    if evidence.get("expected_transition_verified") is not True:
        return blocked("SHARED_TRAILING_EXPECTED_NATIVE_CHANGE_UNVERIFIED", **common)
    return blocked(
        "SHARED_TRAILING_T100_OWNED_STOP_OBSERVED_NONAUTHORIZING",
        manual_and_other_owned_inventory_unchanged=True,
        native_stop_full_volume_observed=True,
        expected_tightening_observed=True,
        **common,
    )
