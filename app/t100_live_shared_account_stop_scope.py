"""Read-only T100-only stop supervision inside an operator-managed MEXC account.

This is a NONAUTHORIZING scope verifier for already-durable T100 checkpoints.
No REST calls, POSTs, database writes, leader permits or wallet assumptions.
The caller must independently verify full-account inventory completeness,
durable checkpoint/revision provenance, intent recovery, and freshness.
An uncheckpointed position is NOT conclusively proven manual by this module.

Existing operator positions may be stopless, but count toward the 4-slot
limit; manual stops remain out of T100's trailing/stop-amendment scope.
No live supervisor or submitter is wired to this diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.t100_live_durable_recovery import (
    DurableRecoveryReport, PositionCheckpoint,
)
from app.t100_live_shared_account_partition import (
    SharedPartitionHalted, partition_shared_account,
)
from app.t100_live_supervisor import (
    LivePositionSupervisor, ManagedShort, SupervisionHalted,
)


@dataclass(frozen=True)
class SharedStopScopeReport:
    status: str
    account_positions: int | None = None
    managed_positions: int | None = None
    manual_positions: int | None = None
    manual_positions_without_positive_sl: int | None = None
    t100_native_stops_observed_non_authorizing: bool = False
    account_collateral_certified: bool = False
    owner_provenance_independently_certified: bool = False
    live_adapter_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


class _FilteredReadOnlyInventory:
    def __init__(self, positions: list[dict[str, Any]],
                 stops: list[dict[str, Any]]) -> None:
        self.positions = positions
        self.stops = stops

    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.positions

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.stops


async def observe_t100_stop_scope_in_shared_account(
    *,
    checkpoints: Sequence[PositionCheckpoint],
    recovered: DurableRecoveryReport | None,
    account_positions: Any,
    account_stops: Any,
) -> SharedStopScopeReport:
    """Scope a strict existing supervisor to recorded T100 IDs only.

    Output does not prove live authorization. Account critical inventory MUST
    be bookended independently; this function never assumes exchange atomicity.
    """
    def blocked(status: str, **counts: Any) -> SharedStopScopeReport:
        return SharedStopScopeReport(status=status, **counts)

    try:
        partition = partition_shared_account(
            checkpoints=checkpoints,
            positions=account_positions,
            stops=account_stops,
        )
    except (SharedPartitionHalted, TypeError, ValueError):
        return blocked("SHARED_STOP_SCOPE_PARTITION_UNVERIFIED")

    common = dict(
        account_positions=partition.account_positions,
        managed_positions=len(partition.managed_positions),
        manual_positions=partition.manual_positions,
        manual_positions_without_positive_sl=(
            partition.manual_positions_without_positive_native_sl
        ),
    )

    # Unknown/stray T100 entries must be rejected by the independent durable
    # ledger/intent reconciliation. A live permit cannot rely on this view.
    if not checkpoints:
        if recovered is not None:
            return blocked("SHARED_STOP_SCOPE_UNEXPECTED_RECOVERY", **common)
        return blocked("SHARED_STOP_SCOPE_MANUAL_ONLY_NONAUTHORIZING", **common)

    if (
        type(recovered) is not DurableRecoveryReport
        or recovered.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
        or recovered.live_authorization is not False
        or type(recovered.recovered) is not tuple
        or len(recovered.recovered) != len(checkpoints)
        or recovered.checkpoint_count != len(checkpoints)
        or recovered.exchange_position_count != len(checkpoints)
        or recovered.exchange_stop_count != len(checkpoints)
        or any(type(row) is not ManagedShort for row in recovered.recovered)
    ):
        return blocked("SHARED_STOP_SCOPE_DURABLE_PROVENANCE_UNVERIFIED", **common)

    mapped = {
        (row.symbol, row.position_id, row.stop_order_id)
        for row in recovered.recovered
    }
    wanted = {
        (row.symbol, row.position_id, row.stop_order_id)
        for row in checkpoints
    }
    if len(mapped) != len(checkpoints) or mapped != wanted:
        return blocked("SHARED_STOP_SCOPE_DURABLE_IDENTITY_DRIFT", **common)

    # Reuse the strict, offline-tested native stop price, side and effective
    # protective-volume checks on T100-owned rows only. No manual edits.
    inventory = _FilteredReadOnlyInventory(
        partition.managed_positions, partition.managed_stops,
    )
    try:
        await LivePositionSupervisor(inventory).verify(
            list(recovered.recovered)
        )
    except (SupervisionHalted, TypeError, ValueError):
        return blocked("SHARED_STOP_SCOPE_T100_NATIVE_STOP_UNVERIFIED", **common)
    return blocked(
        "SHARED_STOP_SCOPE_T100_STOPS_OBSERVED_NONAUTHORIZING",
        t100_native_stops_observed_non_authorizing=True,
        **common,
    )
