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
from decimal import Decimal as D,InvalidOperation
from typing import Any

from app.t100_live_durable_recovery import (
    DurableRecoveryReport,PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,FourSlotSnapshotResult,_ledger_digest,_hash_rows,
)
from app.t100_live_native_stop_execution_audit import assess_native_short_market_stop_row
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
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


def _positive_quantity(raw: Any)->D:
    if raw is None or type(raw) is bool:
        raise ValueError("POST_COMMIT_QUANTITY_INVALID")
    try:
        result=D(str(raw))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("POST_COMMIT_QUANTITY_INVALID") from None
    if not result.is_finite() or result<=0:
        raise ValueError("POST_COMMIT_QUANTITY_INVALID")
    return result


class V40PostCommitRestartAuditor:
    """Read-only historical revision check; NEVER unlock live transport."""

    def __init__(
        self,snapshot: FourSlotSnapshotCoordinator,
        *,probe:ReadOnlyMexcProbe|None=None,
    ):
        if type(snapshot) is not FourSlotSnapshotCoordinator:
            raise ValueError("POST_COMMIT_TYPED_SNAPSHOT_REQUIRED")
        if probe is not None and type(probe) is not ReadOnlyMexcProbe:
            raise ValueError("POST_COMMIT_TYPED_GET_ONLY_PROBE_REQUIRED")
        self.snapshot=snapshot
        self.probe=probe

    async def _strict_native_stop_bookend(self,checkpoints:list[PositionCheckpoint])->str:
        """Independent account-wide signed GET witness, never a trade permit."""
        if self.probe is None:
            raise ValueError("POST_COMMIT_GET_ONLY_PROBE_MISSING")
        positions=await self.probe.raw_open_positions()
        stops=await self.probe.raw_open_stops()
        counts=await self.probe.raw_open_order_counts()
        mode=await self.probe.raw_position_mode()
        n=len(checkpoints)
        required_counts=("sumCount","limitOrderCount","stopOrderCount",
                         "planOrderCount","trackOrderCount")
        if (
            type(positions) is not list or type(stops) is not list
            or type(counts) is not dict
            or len(positions)!=n or len(stops)!=n
            or type(mode) is not int or mode!=1
            or any(type(counts.get(k)) is not int or counts[k]<0
                   for k in required_counts)
            or counts["sumCount"]!=n or counts["stopOrderCount"]!=n
            or any(counts[k]!=0 for k in (
                "limitOrderCount","planOrderCount","trackOrderCount"
            ))
            or any(type(x) is not dict for x in positions+stops)
        ):
            raise ValueError("POST_COMMIT_SIGNED_ACCOUNT_TOPOLOGY_INVALID")
        position_by_id={}
        stop_by_id={}
        for item in positions:
            pid=str(item.get("positionId") or "")
            if not re.fullmatch(r"[1-9][0-9]*",pid) or pid in position_by_id:
                raise ValueError("POST_COMMIT_SIGNED_POSITION_IDENTITY_INVALID")
            position_by_id[pid]=item
        for item in stops:
            pid=str(item.get("positionId") or "")
            if not re.fullmatch(r"[1-9][0-9]*",pid) or pid in stop_by_id:
                raise ValueError("POST_COMMIT_SIGNED_STOP_IDENTITY_INVALID")
            stop_by_id[pid]=item
        ids={cp.position_id for cp in checkpoints}
        if (
            len(ids)!=n
            or set(position_by_id)!=ids
            or set(stop_by_id)!=ids
        ):
            raise ValueError("POST_COMMIT_UNOWNED_SIGNED_INVENTORY")
        for cp in checkpoints:
            position=position_by_id[cp.position_id]
            sl=stop_by_id[cp.position_id]
            if (
                position.get("symbol")!=cp.symbol
                or type(position.get("positionType")) is not int
                or position["positionType"]!=2
                or type(position.get("state")) is not int
                or position["state"]!=1
                or type(position.get("openType")) is not int
                or position["openType"]!=2
                or type(position.get("leverage")) is not int
                or position["leverage"]!=1
                or _positive_quantity(position.get("holdVol"))!=cp.contracts
                or str(sl.get("id") or "")!=cp.stop_order_id
                or assess_native_short_market_stop_row(
                    stop=sl,symbol=cp.symbol,position_id=cp.position_id,
                    position_quantity=cp.contracts,
                    maximum_short_stop_price=cp.stop_price,
                ).active_native_full_volume_market_sl_observed is not True
            ):
                raise ValueError("POST_COMMIT_NATIVE_MARKET_SL_NOT_VERIFIED")
        # Full order/position rows are hashed, not just selected critical
        # fields. Every static observation must stay unchanged across the
        # two complete recoveries, including venue-introduced stop fields.
        return _hash_rows({
            "positions":positions,"stops":stops,
            "order_counts":[counts],
            "hedge_mode":[{"positionMode":mode}],
        })

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
        native_stop_checksums=[]
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
            # Recovery returns ManagedShort but omits the checkpoint's
            # actual durable version. Without a direct ledger reread,
            # an equal stop/quantity could be mistakenly accepted from an
            # older revision. Compare the complete independently read-only
            # ledger digest with the snapshot and the full typed checkpoint,
            # including checkpoint_version and immutable order origin.
            try:
                ledger=await self.snapshot.ledger.read_snapshot()
                if _ledger_digest(ledger)!=result.ledger_hash:
                    return halt("POST_COMMIT_LEDGER_READBACK_CHANGED_FREEZE")
                checkpoints=[
                    checkpoint_from_row(row) for row in ledger.checkpoints
                ]
            except Exception:
                return halt("POST_COMMIT_LEDGER_READBACK_UNVERIFIED_FREEZE")
            if (
                len(checkpoints)!=result.occupied_slots
                or sum(cp==expected for cp in checkpoints)!=1
            ):
                return halt("POST_COMMIT_EXPECTED_REVISION_NOT_DURABLE_FREEZE")
            if self.probe is not None:
                try:
                    native_stop_checksums.append(
                        await self._strict_native_stop_bookend(checkpoints)
                    )
                except Exception:
                    return halt("POST_COMMIT_SIGNED_NATIVE_STOP_GET_UNVERIFIED_FREEZE")
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
        if self.probe is not None and (
            len(native_stop_checksums)!=2
            or native_stop_checksums[0]!=native_stop_checksums[1]
        ):
            return halt("POST_COMMIT_NATIVE_STOP_GET_BOOKENDS_DRIFT_FREEZE")
        return V40PostCommitRestartObservation(
            status=(
                "POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING"
                if self.probe is not None else
                "POST_COMMIT_DURABLE_EXCHANGE_RECOVERED_READONLY_NONAUTHORIZING"
            ),
            recovered_slots=len(observations[1][2]),
            expected_checkpoint_recovered=True,
            two_independent_full_account_snapshots_matched=True,
            native_market_stop_type_verified_after_restart=(
                self.probe is not None
            ),
        )
