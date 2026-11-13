"""Unreleased V40 bar-cursor fail-closed checks, no database or MEXC."""
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar, V40ExitCheckpoint, propose_next_completed_bar,
)
from app.t100_v40_unreleased_bar_cursor import (
    V40BarCursorState, V40BarCursorHalted, V40DurableBarCursor, _state,
)

FILL_MS = 1_800_001_234
FILL_CEIL_SECOND = 1_800_002
FIRST_COMPLETE_BAR = 1_801_800


def cursor(**changes):
    values = dict(
        position_id="10",entry_external_oid="entry-10",
        signed_fill_at_ms=FILL_MS,last_committed_open_ts=None,
        pending_open_ts=None,pending_status=None,pending_stop_price=None,
        checkpoint_version=1,stop_order_id="11",
        confirmed_stop_price=D("175"),best_profit_pct=D("0"),
        trail_armed=False,cursor_revision=1,
    )
    values.update(changes)
    return V40BarCursorState(**values)


def policy():
    cp = V40ExitCheckpoint(
        position_id="10",symbol="EXAMPLE_USDT",entry_price=D("100"),
        filled_at_ts=FILL_CEIL_SECOND,native_stop_price=D("175"),
    )
    candle=V40CompletedBar(
        open_ts=FIRST_COMPLETE_BAR,open=D("100"),
        high=D("100"),low=D("95"),close=D("96"),
    )
    return cp,propose_next_completed_bar(
        cp,candle,price_tick=D("0.01"),now_ts=FIRST_COMPLETE_BAR+1800,
    )


class SpyConn:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "RETURNING position_id" in sql:
            return {"position_id": "10"}
        return None


@pytest.mark.asyncio
async def test_cursor_stage_uses_ceiled_fill_milliseconds_and_exact_bar():
    conn=SpyConn()
    cp,plan=policy()
    await V40DurableBarCursor(conn).stage(
        cursor=cursor(),previous=cp,plan=plan,
    )
    assert len(conn.calls)==1
    assert "pending_open_ts" in conn.calls[0][0]
    assert conn.calls[0][1][2]==FIRST_COMPLETE_BAR


@pytest.mark.asyncio
async def test_cursor_rejects_flooring_fill_milliseconds_into_earlier_bar():
    conn=SpyConn()
    cp,plan=policy()
    fake_floor=V40ExitCheckpoint(
        position_id="10",symbol="EXAMPLE_USDT",entry_price=D("100"),
        filled_at_ts=FILL_CEIL_SECOND-1,native_stop_price=D("175"),
    )
    with pytest.raises(V40BarCursorHalted,match="PLAN_OR_OWNER_DRIFT"):
        await V40DurableBarCursor(conn).stage(
            cursor=cursor(),previous=fake_floor,plan=plan,
        )
    assert conn.calls == []


@pytest.mark.asyncio
async def test_pending_cursor_cannot_stage_again_after_crash():
    conn=SpyConn()
    cp,plan=policy()
    with pytest.raises(V40BarCursorHalted,match="PLAN_OR_OWNER_DRIFT"):
        await V40DurableBarCursor(conn).stage(
            cursor=cursor(
                pending_open_ts=FIRST_COMPLETE_BAR,
                pending_status="KEEP_VERIFIED_NATIVE_STOP",
                pending_stop_price=D("175"),
            ),
            previous=cp,plan=plan,
        )
    assert not conn.calls


@pytest.mark.asyncio
async def test_cursor_commit_needs_matching_durable_checkpoint_and_signed_sl():
    conn=SpyConn()
    _,plan=policy()
    cp=PositionCheckpoint(
        symbol="EXAMPLE_USDT",external_oid="entry-10",
        exchange_order_id="9",position_id="10",
        contracts=D("1"),entry_price=D("100"),
        stop_order_id="11",stop_price=D("175"),
        best_profit_pct=D("0"),armed=False,lifecycle_state="OPEN",
        checkpoint_version=1,
    )
    with pytest.raises(V40BarCursorHalted,match="COMMIT_UNVERIFIED"):
        await V40DurableBarCursor(conn).commit_verified(
            cursor=cursor(
                pending_open_ts=FIRST_COMPLETE_BAR,
                pending_status="KEEP_VERIFIED_NATIVE_STOP",
                pending_stop_price=D("175"),
            ),
            plan=plan,persisted_checkpoint=cp,
            independent_native_sl_get_verified=False,
            immutable_checkpoint_history_verified=False,
        )
    assert conn.calls==[]


def test_cursor_row_validation_rejects_unknown_or_untrusted_state():
    row = dict(
        position_id="10",entry_external_oid="entry-10",
        signed_fill_at_ms=FILL_MS,last_committed_open_ts=None,
        pending_open_ts=None,pending_status=None,pending_stop_price=None,
        checkpoint_version=1,stop_order_id="11",
        confirmed_stop_price=D("175"),best_profit_pct=D("15"),
        trail_armed=False,cursor_revision=1,
    )
    with pytest.raises(V40BarCursorHalted,match="ROW_INVALID"):
        _state(row)
