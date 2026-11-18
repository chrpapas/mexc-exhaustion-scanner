"""Frozen 14% arm + tick-rounded high-watermark: real immutable unchanged BAR.

No order or account mutations; must be reproducible from exact completed Min30.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_checkpoint_journal import (
    BAR_OBSERVED,CheckpointJournalHalted,
    build_verified_bar_checkpoint,validate_checkpoint_transition,
)
from app.t100_live_durable_recovery import (
    DurableRecoveryHalted,PositionCheckpoint,recover_nonflat,
)
from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar,V40ExitCheckpoint,propose_next_completed_bar,
)
from app.t100_v40_unreleased_bar_cursor import (
    V40BarCursorHalted,V40DurableBarCursor,
)
from tests.test_t100_v40_immutable_bar_checkpoint import signed_bar_evidence
from tests.test_t100_v40_unreleased_bar_cursor import (
    SpyConn,cursor,FIRST_COMPLETE_BAR,FILL_CEIL_SECOND,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture


def armed_evidence():
    cp,stop=signed_bar_evidence()
    cp=replace(cp,stop_price=D("85"),best_profit_pct=D("15"),
               armed=True,checkpoint_version=2)
    stop={**stop,"stopLossPrice":D("85")}
    return cp,stop


def model():
    previous=V40ExitCheckpoint(
        position_id="10",symbol="SIREN_USDT",entry_price=D("100"),
        filled_at_ts=FILL_CEIL_SECOND,native_stop_price=D("85"),
        best_profit_pct=D("15"),trail_armed=True,
    )
    bar=V40CompletedBar(
        open_ts=FIRST_COMPLETE_BAR,open=D("84.95"),
        high=D("84.99"),low=D("84.9"),close=D("84.95"),
    )
    # Raw stop at 85.4, rounds UP to 86 at tick 1. Existing native
    # stop 85 is tighter: best still increases from 15 to 15.1.
    decision=propose_next_completed_bar(
        previous,bar,price_tick=D("1"),
        now_ts=FIRST_COMPLETE_BAR+1800,
    )
    return previous,decision


def test_armed_best_increase_without_native_stop_change_is_checkpoint_revision():
    cp,stop=armed_evidence()
    _,decision=model()
    assert decision.status=="KEEP_VERIFIED_NATIVE_STOP"
    assert decision.proposed_checkpoint.best_profit_pct==D("15.1")
    assert decision.proposed_checkpoint.native_stop_price==D("85")
    revised=build_verified_bar_checkpoint(
        cp,stop=stop,best_profit_pct=D("15.1"),
    )
    assert revised.armed and revised.stop_price==D("85")
    assert revised.checkpoint_version==3
    validate_checkpoint_transition(cp,revised,reason=BAR_OBSERVED)


@pytest.mark.parametrize("best,armed",[
    ("15",True),("14.9",True),("15.1",False),("NaN",True),
])
def test_armed_revision_cannot_regress_or_lose_arm(best,armed):
    cp,stop=armed_evidence()
    if not armed:
        target=replace(cp,armed=False,best_profit_pct=D(best))
        with pytest.raises(CheckpointJournalHalted):
            validate_checkpoint_transition(cp,target,reason=BAR_OBSERVED)
    else:
        with pytest.raises((CheckpointJournalHalted,ArithmeticError)):
            build_verified_bar_checkpoint(
                cp,stop=stop,best_profit_pct=D(best),
            )


@pytest.mark.asyncio
async def test_armed_cursor_requires_completed_best_revision_before_commit():
    _,plan=model()
    state=cursor(
        checkpoint_version=2,stop_order_id="11",
        confirmed_stop_price=D("85"),best_profit_pct=D("15"),
        trail_armed=True,pending_open_ts=FIRST_COMPLETE_BAR,
        pending_status="KEEP_VERIFIED_NATIVE_STOP",
        pending_stop_price=D("85"),
        pending_best_profit_pct=D("15.1"),pending_trail_armed=True,
    )
    initial=PositionCheckpoint(
        symbol="SIREN_USDT",external_oid="entry-10",
        exchange_order_id="9",position_id="10",
        contracts=D("1"),entry_price=D("100"),
        stop_order_id="11",stop_price=D("85"),
        best_profit_pct=D("15"),armed=True,
        lifecycle_state="OPEN",checkpoint_version=2,
    )
    db=SpyConn()
    with pytest.raises(V40BarCursorHalted,match="COMMIT_UNVERIFIED"):
        await V40DurableBarCursor(db).commit_verified(
            cursor=state,plan=plan,persisted_checkpoint=initial,
            independent_native_sl_get_verified=True,
            immutable_checkpoint_history_verified=True,
        )
    assert not db.calls
    with pytest.raises(V40BarCursorHalted,match="COMMIT_CAS_FAILED"):
        await V40DurableBarCursor(db).commit_verified(
            cursor=state,plan=plan,
            persisted_checkpoint=replace(
                initial,best_profit_pct=D("15.1"),checkpoint_version=3,
            ),
            independent_native_sl_get_verified=True,
            immutable_checkpoint_history_verified=True,
        )
    assert len(db.calls)==1


def test_restart_validates_armed_bar_improved_best_with_native_stop_intact():
    ld,orders,positions,stops=fixture(1)
    base=dict(ld.revisions[0])
    row=ld.checkpoints[0]
    # Original entry revision, exact stop-tighter revision that arms 15%,
    # followed by the unchanged native SL BAR_OBSERVED best revision.
    v2={**base,"checkpoint_version":2,"write_reason":"STOP_VERIFIED",
        "stop_price":D("85"),"best_profit_pct":D("15"),"armed":True}
    v3={**v2,"checkpoint_version":3,"write_reason":"BAR_OBSERVED",
        "best_profit_pct":D("15.1")}
    ld.revisions.extend([v2,v3])
    row.update(checkpoint_version=3,stop_price=D("85"),
               best_profit_pct=D("15.1"),armed=True)
    stops[0]["stopLossPrice"]=D("85")
    verified_amendment={
        "external_id":"v40-test-arm85",
        "symbol":row["symbol"],
        "position_id":row["position_id"],
        "stop_order_id":str(base["stop_order_id"]),
        "verified_stop_order_id":str(base["stop_order_id"]),
        "previous_stop":D(str(base["stop_price"])),
        "proposed_stop":D("85"),
        "state":"VERIFIED",
    }
    try:
        result=recover_nonflat(
            checkpoint_rows=ld.checkpoints,revision_rows=ld.revisions,
            intent_rows=ld.intents,
            stop_amendment_rows=[verified_amendment],
            orders=orders,positions=positions,stops=stops,
            positions_complete=True,stops_complete=True,
        )
    except DurableRecoveryHalted as exc:
        # Any exchange/lot fixture incompatibility must be detected; do
        # not convert that into a fabricated successful recovery.
        raise AssertionError(f"independent recovery rejected: {exc}") from None
    assert len(result.recovered)==1
    assert result.recovered[0].best_profit_pct==D("15.1")
    assert result.recovered[0].armed is True
