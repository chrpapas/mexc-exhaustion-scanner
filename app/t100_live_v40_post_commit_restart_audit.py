"""V40 read-only post-commit durable/exchange restart observation.

The stop completion handoff commits a journal and checkpoint only with a
future certified transport permit, and requires a fresh restart check.
This is an OFF-by-default, unregistered read-only verification stage for
that subsequent checkpoint, reusing the existing full-account 1–4-slot
read-only ledger/venue origin-order reconciliation.

Two complete, separate four-slot snapshot audits must match; one in-memory
checkpoint write ACK is not restart recovery. This does not prove process
restart, MEXC GET signing provenance, native MARKET stop semantics, gap
protection or authorization for new entries/mutations. No exchange POST or
database write methods are present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.t100_live_durable_recovery import (
    DurableRecoveryReport,PositionCheckpoint,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,FourSlotSnapshotResult,
)
from app.t100_live_supervisor import ManagedShort


@dataclass(frozen=True)
class V40PostCommitRestartObservation:
    status: str
    recovered_slots: int=0
    expected_checkpoint_recovered: bool=False
    two_independent_full_account_snapshots_matched: bool=False
    independent_process_restart_verified: bool=False
    native_market_stop_type_verified_after_restart: bool=False
    exchange_stop_atomicity_verified: bool=False
    signed_get_transport_provenance_verified: bool=False
    entry_allowed: bool=False
    stop_mutation_allowed: bool=False
    live_authorization: bool=False
    exchange_posts: int=0
    db_writes: int=0


def _recovered_identity(report: DurableRecoveryReport) -> tuple:
    if type(report) is not DurableRecoveryReport:
        raise ValueError("POST_COMMIT_RECOVERY_SHAPE")
    if (
        type(report.checkpoint_count) is not int
        or not 1<=report.checkpoint_count<=4
        or type(report.exchange_position_count) is not int
        or type(report.exchange_stop_count) is not int
        or report.exchange_position_count!=report.checkpoint_count
        or report.exchange_stop_count!=report.checkpoint_count
        or type(report.recovered) is not tuple
        or len(report.recovered)!=report.checkpoint_count
        or any(type(x) is not ManagedShort for x in report.recovered)
    ):
        raise ValueError("POST_COMMIT_RECOVERY_INCOMPLETE")
    keys=[(r.symbol,r.position_id) for r in report.recovered]
    if len(set(keys))!=len(keys):
        raise ValueError("POST_COMMIT_DUPLICATE_RECOVERY")
    return tuple(sorted(
        (r.symbol,r.position_id,r.contracts,r.entry_price,
         r.stop_order_id,r.stop_price,r.best_profit_pct,r.armed)
        for r in report.recovered
    ))


class V40PostCommitRestartAuditor:
    """Read-only historical revision check; NEVER unlock live transport."""

    def __init__(self, snapshot: FourSlotSnapshotCoordinator):
        if type(snapshot) is not FourSlotSnapshotCoordinator:
            raise ValueError("POST_COMMIT_TYPED_SNAPSHOT_REQUIRED")
        self.snapshot=snapshot

    async def observe(
        self,*,expected: PositionCheckpoint,
    ) -> V40PostCommitRestartObservation:
        def halt(reason: str)->V40PostCommitRestartObservation:
            return V40PostCommitRestartObservation(status=reason)

        if (
            type(expected) is not PositionCheckpoint
            or expected.lifecycle_state!="OPEN"
            or type(expected.checkpoint_version) is not int
            or expected.checkpoint_version<2
            or not re.fullmatch(r"[A-Z0-9]+_USDT",expected.symbol)
            or not re.fullmatch(r"[1-9][0-9]*",expected.position_id)
            or not re.fullmatch(r"[1-9][0-9]*",expected.stop_order_id)
            or expected.armed is not True
            or expected.best_profit_pct<14
        ):
            return halt("POST_COMMIT_EXPECTED_CHECKPOINT_INVALID_FREEZE")

        observations=[]
        for _ in range(2):
            try:
                result=await self.snapshot.inspect(shared_manual_observation=False)
            except Exception:
                return halt("POST_COMMIT_READONLY_SNAPSHOT_FAILED_FREEZE")
            if (
                type(result) is not FourSlotSnapshotResult
                or result.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
                or type(result.occupied_slots) is not int
                or not 1<=result.occupied_slots<=4
                or type(result.ledger_hash) is not str
                or not re.fullmatch(r"[a-f0-9]{64}",result.ledger_hash)
                or type(result.critical_inventory_hash) is not str
                or not re.fullmatch(r"[a-f0-9]{64}",result.critical_inventory_hash)
                or result.live_authorization is not False
                or result.entry_allowed is not False
            ):
                return halt("POST_COMMIT_RESTART_PROVENANCE_UNVERIFIED_FREEZE")
            try:
                identities=_recovered_identity(result.recovery)
            except Exception:
                return halt("POST_COMMIT_RESTART_POSITION_RECOVERY_INVALID_FREEZE")
            if result.occupied_slots!=len(identities):
                return halt("POST_COMMIT_RESTART_SLOT_COUNT_INVALID_FREEZE")
            matching=[
                r for r in result.recovery.recovered
                if r.symbol==expected.symbol and r.position_id==expected.position_id
            ]
            if len(matching)!=1:
                return halt("POST_COMMIT_EXPECTED_POSITION_NOT_RECOVERED_FREEZE")
            current=matching[0]
            if not (
                current.contracts==expected.contracts
                and current.entry_price==expected.entry_price
                and current.stop_order_id==expected.stop_order_id
                and current.stop_price==expected.stop_price
                and current.best_profit_pct==expected.best_profit_pct
                and current.armed is expected.armed
            ):
                return halt("POST_COMMIT_RECOVERED_CHECKPOINT_DRIFT_FREEZE")
            observations.append((
                result.ledger_hash,result.critical_inventory_hash,
                identities,
            ))
        if observations[0]!=observations[1]:
            return halt("POST_COMMIT_INDEPENDENT_SNAPSHOT_DRIFT_FREEZE")
        return V40PostCommitRestartObservation(
            status="POST_COMMIT_DURABLE_EXCHANGE_RECOVERED_READONLY_NONAUTHORIZING",
            recovered_slots=len(observations[1][2]),
            expected_checkpoint_recovered=True,
            two_independent_full_account_snapshots_matched=True,
        )
