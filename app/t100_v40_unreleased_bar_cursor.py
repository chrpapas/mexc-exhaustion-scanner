"""UNRELEASED CAS-backed V40 completed-Min30 bar cursor.

Keeps one pending model decision per owned position across process restarts.
This is NOT the stop journal, not an exchange witness and not a worker.
A proposed bar can be committed only after the corresponding native stop AND
new durable position checkpoint (including updated best/armed state) are
independently verified and consistent. Unknown outcomes stay pending forever
until explicitly reconciled; no retry of stop POST is implied.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_v40_unreleased_exit_policy import (
    BAR_SECONDS,V40ExitCheckpoint,V40ExitDecision,
)


class V40BarCursorHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40BarCursorState:
    position_id: str
    entry_external_oid: str
    signed_fill_at_ms: int
    last_committed_open_ts: int | None
    pending_open_ts: int | None
    pending_status: str | None
    pending_stop_price: D | None
    pending_best_profit_pct: D | None
    pending_trail_armed: bool | None
    checkpoint_version: int
    stop_order_id: str
    confirmed_stop_price: D
    best_profit_pct: D
    trail_armed: bool
    cursor_revision: int


def _state(row: Any) -> V40BarCursorState:
    try:
        result=V40BarCursorState(
            position_id=str(row["position_id"]),
            entry_external_oid=str(row["entry_external_oid"]),
            signed_fill_at_ms=int(row["signed_fill_at_ms"]),
            last_committed_open_ts=row["last_committed_open_ts"],
            pending_open_ts=row["pending_open_ts"],
            pending_status=row["pending_status"],
            pending_stop_price=(
                D(str(row["pending_stop_price"]))
                if row["pending_stop_price"] is not None else None
            ),
            pending_best_profit_pct=(
                D(str(row["pending_best_profit_pct"]))
                if row["pending_best_profit_pct"] is not None else None
            ),
            pending_trail_armed=row["pending_trail_armed"],
            checkpoint_version=int(row["checkpoint_version"]),
            stop_order_id=str(row["stop_order_id"]),
            confirmed_stop_price=D(str(row["confirmed_stop_price"])),
            best_profit_pct=D(str(row["best_profit_pct"])),
            trail_armed=row["trail_armed"],
            cursor_revision=int(row["cursor_revision"]),
        )
    except (KeyError,TypeError,ValueError,ArithmeticError):
        raise V40BarCursorHalted("V40_BAR_CURSOR_ROW_INVALID") from None
    if not (
        result.position_id.isascii()
        and result.position_id.isdecimal()
        and result.position_id[0] != "0"
        and bool(result.entry_external_oid)
        and result.signed_fill_at_ms > 0
        and result.checkpoint_version > 0
        and result.cursor_revision > 0
        and result.confirmed_stop_price.is_finite()
        and result.confirmed_stop_price > 0
        and result.best_profit_pct.is_finite()
        and result.best_profit_pct >= 0
        and type(result.trail_armed) is bool
        and result.trail_armed == (result.best_profit_pct >= D("14"))
        and (
            result.last_committed_open_ts is None
            or type(result.last_committed_open_ts) is int
            and result.last_committed_open_ts % BAR_SECONDS == 0
        )
        and (
            result.pending_open_ts is None
            or type(result.pending_open_ts) is int
            and result.pending_open_ts % BAR_SECONDS == 0
        )
        and (
            result.pending_open_ts is None
            and result.pending_status is None
            and result.pending_stop_price is None
            and result.pending_best_profit_pct is None
            and result.pending_trail_armed is None
            or
            result.pending_open_ts is not None
            and result.pending_status in (
                "KEEP_VERIFIED_NATIVE_STOP",
                "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED",
            )
            and result.pending_stop_price is not None
            and result.pending_stop_price.is_finite()
            and result.pending_stop_price > 0
            and result.pending_best_profit_pct is not None
            and result.pending_best_profit_pct.is_finite()
            and result.pending_best_profit_pct >= result.best_profit_pct
            and result.pending_trail_armed is (
                result.pending_best_profit_pct >= D("14")
            )
            and (
                result.last_committed_open_ts is None
                or result.pending_open_ts > result.last_committed_open_ts
            )
        )
    ):
        raise V40BarCursorHalted("V40_BAR_CURSOR_ROW_INVALID")
    return result


class V40DurableBarCursor:
    """Requires a dedicated reviewed writer and serialized account lease."""

    def __init__(self, conn: Any):
        if not callable(getattr(conn,"fetchrow",None)):
            raise V40BarCursorHalted("V40_BAR_CURSOR_DB_UNAVAILABLE")
        self.conn=conn

    async def read(self, position_id: str) -> V40BarCursorState:
        try:
            row=await self.conn.fetchrow(
                """SELECT * FROM public.t100_v40_exit_bar_cursor
                   WHERE position_id=$1""",position_id,
            )
        except Exception:
            raise V40BarCursorHalted("V40_BAR_CURSOR_READ_FAILED") from None
        if row is None:
            raise V40BarCursorHalted("V40_BAR_CURSOR_NOT_ENROLLED")
        return _state(row)

    async def enroll(
        self,*, checkpoint:PositionCheckpoint, signed_fill_at_ms:int,
        independent_fill_time_verified:bool,
        full_native_sl_signed_get_verified:bool,
    ) -> V40BarCursorState:
        if not (
            type(checkpoint) is PositionCheckpoint
            and checkpoint.lifecycle_state=="OPEN"
            and checkpoint.checkpoint_version>=1
            and type(signed_fill_at_ms) is int
            and signed_fill_at_ms>0
            and independent_fill_time_verified is True
            and full_native_sl_signed_get_verified is True
        ):
            raise V40BarCursorHalted("V40_BAR_CURSOR_ENROLL_NOT_VERIFIED")
        try:
            await self.conn.fetchrow(
                """INSERT INTO public.t100_v40_exit_bar_cursor
                   (position_id,entry_external_oid,signed_fill_at_ms,
                    checkpoint_version,stop_order_id,confirmed_stop_price,
                    best_profit_pct,trail_armed)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (position_id) DO NOTHING
                   RETURNING position_id""",
                checkpoint.position_id, checkpoint.external_oid,
                signed_fill_at_ms, checkpoint.checkpoint_version,
                checkpoint.stop_order_id, checkpoint.stop_price,
                checkpoint.best_profit_pct, checkpoint.armed,
            )
        except Exception:
            raise V40BarCursorHalted("V40_BAR_CURSOR_ENROLL_WRITE_FAILED") from None
        cursor=await self.read(checkpoint.position_id)
        if (
            cursor.entry_external_oid!=checkpoint.external_oid
            or cursor.signed_fill_at_ms!=signed_fill_at_ms
            or cursor.checkpoint_version!=checkpoint.checkpoint_version
            or cursor.stop_order_id!=checkpoint.stop_order_id
            or cursor.confirmed_stop_price!=checkpoint.stop_price
            or cursor.best_profit_pct!=checkpoint.best_profit_pct
            or cursor.trail_armed!=checkpoint.armed
        ):
            raise V40BarCursorHalted("V40_BAR_CURSOR_EXISTING_OWNER_ORIGIN_DRIFT")
        return cursor

    async def stage(
        self,*, cursor:V40BarCursorState,
        previous:V40ExitCheckpoint,plan:V40ExitDecision,
    ) -> None:
        candidate=plan.proposed_checkpoint
        if not (
            type(cursor) is V40BarCursorState
            and type(previous) is V40ExitCheckpoint
            and type(plan) is V40ExitDecision
            and candidate is not None
            and plan.status in (
                "KEEP_VERIFIED_NATIVE_STOP",
                "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED",
            )
            and candidate.position_id==previous.position_id==cursor.position_id
            and (cursor.signed_fill_at_ms+999)//1000==previous.filled_at_ts
            and cursor.pending_open_ts is None
            and cursor.last_committed_open_ts==previous.last_completed_open_ts
            and cursor.confirmed_stop_price==previous.native_stop_price
            and cursor.best_profit_pct==previous.best_profit_pct
            and cursor.trail_armed==previous.trail_armed
            and candidate.last_completed_open_ts is not None
            and candidate.last_completed_open_ts==(
                cursor.last_committed_open_ts+BAR_SECONDS
                if cursor.last_committed_open_ts is not None
                else ((previous.filled_at_ts+BAR_SECONDS-1)//BAR_SECONDS)*BAR_SECONDS
            )
        ):
            raise V40BarCursorHalted("V40_BAR_CURSOR_PLAN_OR_OWNER_DRIFT")
        try:
            row=await self.conn.fetchrow(
                """UPDATE public.t100_v40_exit_bar_cursor
                   SET pending_open_ts=$3,pending_status=$4,
                       pending_stop_price=$5,pending_best_profit_pct=$6,
                       pending_trail_armed=$7,updated_at=clock_timestamp()
                   WHERE position_id=$1 AND cursor_revision=$2
                     AND pending_open_ts IS NULL
                   RETURNING position_id""",
                cursor.position_id,cursor.cursor_revision,
                candidate.last_completed_open_ts,plan.status,
                candidate.native_stop_price,candidate.best_profit_pct,
                candidate.trail_armed,
            )
        except Exception:
            raise V40BarCursorHalted("V40_BAR_CURSOR_STAGE_FAILED") from None
        if row is None:
            raise V40BarCursorHalted("V40_BAR_CURSOR_STALE_OR_ALREADY_PENDING")

    async def commit_verified(
        self,*,cursor:V40BarCursorState,
        plan:V40ExitDecision,
        persisted_checkpoint:PositionCheckpoint,
        independent_native_sl_get_verified:bool,
        immutable_checkpoint_history_verified:bool,
    ) -> V40BarCursorState:
        candidate=plan.proposed_checkpoint
        if not (
            type(cursor) is V40BarCursorState
            and type(plan) is V40ExitDecision
            and type(persisted_checkpoint) is PositionCheckpoint
            and candidate is not None
            and cursor.pending_open_ts==candidate.last_completed_open_ts
            and cursor.pending_status==plan.status
            and cursor.pending_stop_price==candidate.native_stop_price
            and cursor.pending_best_profit_pct==candidate.best_profit_pct
            and cursor.pending_trail_armed==candidate.trail_armed
            and candidate.best_profit_pct >= cursor.best_profit_pct
            and candidate.position_id==cursor.position_id==persisted_checkpoint.position_id
            and cursor.entry_external_oid==persisted_checkpoint.external_oid
            and persisted_checkpoint.lifecycle_state=="OPEN"
            and persisted_checkpoint.stop_price==candidate.native_stop_price
            and persisted_checkpoint.best_profit_pct<=candidate.best_profit_pct
            and persisted_checkpoint.checkpoint_version>=cursor.checkpoint_version
            and persisted_checkpoint.stop_price<=cursor.confirmed_stop_price
            and (
                (
                    plan.status=="KEEP_VERIFIED_NATIVE_STOP"
                    and candidate.native_stop_price==cursor.confirmed_stop_price
                    and persisted_checkpoint.checkpoint_version==cursor.checkpoint_version
                    and persisted_checkpoint.stop_order_id==cursor.stop_order_id
                    and persisted_checkpoint.best_profit_pct<=cursor.best_profit_pct
                )
                or
                (
                    plan.status=="NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
                    and candidate.native_stop_price<cursor.confirmed_stop_price
                    and persisted_checkpoint.checkpoint_version==cursor.checkpoint_version+1
                    and persisted_checkpoint.best_profit_pct==candidate.best_profit_pct
                    and persisted_checkpoint.armed==candidate.trail_armed
                )
            )
            and independent_native_sl_get_verified is True
            and immutable_checkpoint_history_verified is True
        ):
            raise V40BarCursorHalted("V40_BAR_CURSOR_COMMIT_UNVERIFIED")
        try:
            row=await self.conn.fetchrow(
                """UPDATE public.t100_v40_exit_bar_cursor
                   SET last_committed_open_ts=pending_open_ts,
                       pending_open_ts=NULL,pending_status=NULL,
                       pending_stop_price=NULL,
                       pending_best_profit_pct=NULL,pending_trail_armed=NULL,
                       checkpoint_version=$3,stop_order_id=$4,
                       confirmed_stop_price=$5,best_profit_pct=$6,
                       trail_armed=$7,cursor_revision=cursor_revision+1,
                       updated_at=clock_timestamp()
                   WHERE position_id=$1 AND cursor_revision=$2
                     AND pending_open_ts=$8
                     AND pending_stop_price=$5
                     AND pending_best_profit_pct=$6
                     AND pending_trail_armed=$7
                   RETURNING *""",
                cursor.position_id,cursor.cursor_revision,
                persisted_checkpoint.checkpoint_version,
                persisted_checkpoint.stop_order_id,persisted_checkpoint.stop_price,
                candidate.best_profit_pct,candidate.trail_armed,
                candidate.last_completed_open_ts,
            )
        except Exception:
            raise V40BarCursorHalted("V40_BAR_CURSOR_COMMIT_WRITE_FAILED") from None
        if row is None:
            raise V40BarCursorHalted("V40_BAR_CURSOR_COMMIT_CAS_FAILED")
        return _state(row)
