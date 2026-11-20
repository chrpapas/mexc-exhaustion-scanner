"""Immutable BAR_OBSERVED high-watermark consistency, no venue mutations."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_checkpoint_journal import (
    BAR_OBSERVED,CheckpointJournalHalted,
    build_verified_bar_checkpoint,validate_checkpoint_transition,
)
from app.t100_live_durable_recovery import DurableRecoveryHalted,recover_nonflat
from app.t100_v40_unreleased_bar_cursor import V40DurableBarCursor,V40BarCursorHalted
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_v40_four_slot_guardian_policy import evidence
from tests.test_t100_v40_unreleased_bar_cursor import (
    SpyConn,cursor,policy,FIRST_COMPLETE_BAR,
)


def signed_bar_evidence():
    _,ledger,_,stops=evidence(1)
    from app.t100_live_durable_recovery import checkpoint_from_row
    return checkpoint_from_row(ledger.checkpoints[0]),stops[0]


def test_prearm_best_plus_five_requires_same_current_native_stop_full_qty():
    cp,stop=signed_bar_evidence()
    current=build_verified_bar_checkpoint(
        cp,stop=stop,best_profit_pct=D("5"),
    )
    assert current.best_profit_pct==D("5")
    assert current.stop_price==cp.stop_price
    assert current.stop_order_id==cp.stop_order_id
    assert current.armed is False
    assert current.checkpoint_version==cp.checkpoint_version+1
    validate_checkpoint_transition(cp,current,reason=BAR_OBSERVED)


@pytest.mark.parametrize("change",[
    {"id":"999"},{"stopLossPrice":D("176")},
    {"stopLossType":1},{"stopLossReverse":1},
    {"errorCode":1000},{"vol":D("19")},
    {"positionId":"999"},{"orderId":"999"},
])
def test_unmatched_stop_cannot_create_prearm_bar_revision(change):
    cp,stop=signed_bar_evidence()
    with pytest.raises(CheckpointJournalHalted):
        build_verified_bar_checkpoint(
            cp,stop={**stop,**change},best_profit_pct=D("5"),
        )


@pytest.mark.parametrize("best",[
    D("0"),D("14"),D("15"),D("-1"),D("NaN"),
])
def test_unobserved_or_armed_profit_cannot_be_cursor_only_revision(best):
    cp,stop=signed_bar_evidence()
    with pytest.raises((CheckpointJournalHalted,ArithmeticError)):
        build_verified_bar_checkpoint(cp,stop=stop,best_profit_pct=best)


def test_immutable_restart_rebuilds_best_without_any_stop_amendment():
    ld,orders,positions,stops=fixture(1)
    cp=ld.checkpoints[0]
    original=ld.revisions[0]
    cp["checkpoint_version"]=2
    cp["best_profit_pct"]=D("5")
    ld.revisions.append({
        **original,"checkpoint_version":2,
        "write_reason":"BAR_OBSERVED",
        "best_profit_pct":D("5"),
    })
    result=recover_nonflat(
        checkpoint_rows=ld.checkpoints,revision_rows=ld.revisions,
        intent_rows=ld.intents,stop_amendment_rows=[],
        orders=orders,positions=positions,stops=stops,
        positions_complete=True,stops_complete=True,
    )
    assert len(result.recovered)==1
    assert result.recovered[0].best_profit_pct==D("5")
    assert result.recovered[0].stop_price==D("175")
    assert result.recovered[0].armed is False


@pytest.mark.parametrize("change",[
    {"stop_price":D("174")},
    {"stop_order_id":"888"},
    {"best_profit_pct":D("0")},
    {"best_profit_pct":D("14"),"armed":True},
])
def test_restart_rejects_relabelled_bar_revision_or_stop_mutation(change):
    ld,orders,positions,stops=fixture(1)
    original=ld.revisions[0]
    ld.checkpoints[0].update(checkpoint_version=2,best_profit_pct=D("5"))
    ld.revisions.append({
        **original,"checkpoint_version":2,"write_reason":"BAR_OBSERVED",
        "best_profit_pct":D("5"),**change,
    })
    with pytest.raises(DurableRecoveryHalted):
        recover_nonflat(
            checkpoint_rows=ld.checkpoints,revision_rows=ld.revisions,
            intent_rows=ld.intents,stop_amendment_rows=[],
            orders=orders,positions=positions,stops=stops,
            positions_complete=True,stops_complete=True,
        )


@pytest.mark.asyncio
async def test_durable_cursor_rejects_best_change_with_stale_checkpoint():
    previous,planned=policy()
    current=cursor(
        pending_open_ts=FIRST_COMPLETE_BAR,
        pending_status="KEEP_VERIFIED_NATIVE_STOP",
        pending_stop_price=D("175"),
        pending_best_profit_pct=D("5"),pending_trail_armed=False,
    )
    old_checkpoint=__import__(
        "app.t100_live_durable_recovery",fromlist=["PositionCheckpoint"]
    ).PositionCheckpoint(
        symbol="EXAMPLE_USDT",external_oid="entry-10",
        exchange_order_id="9",position_id="10",
        contracts=D("1"),entry_price=D("100"),stop_order_id="11",
        stop_price=D("175"),best_profit_pct=D("0"),
        armed=False,lifecycle_state="OPEN",checkpoint_version=1,
    )
    db=SpyConn()
    with pytest.raises(V40BarCursorHalted,match="COMMIT_UNVERIFIED"):
        await V40DurableBarCursor(db).commit_verified(
            cursor=current,plan=planned,persisted_checkpoint=old_checkpoint,
            independent_native_sl_get_verified=True,
            immutable_checkpoint_history_verified=True,
        )
    assert db.calls==[]


@pytest.mark.asyncio
async def test_durable_cursor_accepts_new_immutable_bar_revision_prior_to_cas():
    _,planned=policy()
    current=cursor(
        pending_open_ts=FIRST_COMPLETE_BAR,
        pending_status="KEEP_VERIFIED_NATIVE_STOP",
        pending_stop_price=D("175"),
        pending_best_profit_pct=D("5"),pending_trail_armed=False,
    )
    from app.t100_live_durable_recovery import PositionCheckpoint
    cp=PositionCheckpoint(
        symbol="EXAMPLE_USDT",external_oid="entry-10",
        exchange_order_id="9",position_id="10",
        contracts=D("1"),entry_price=D("100"),stop_order_id="11",
        stop_price=D("175"),best_profit_pct=D("5"),
        armed=False,lifecycle_state="OPEN",checkpoint_version=2,
    )
    db=SpyConn()
    # Spy DB returns no row; the policy must get as far as the CAS query.
    with pytest.raises(V40BarCursorHalted,match="COMMIT_CAS_FAILED"):
        await V40DurableBarCursor(db).commit_verified(
            cursor=current,plan=planned,persisted_checkpoint=cp,
            independent_native_sl_get_verified=True,
            immutable_checkpoint_history_verified=True,
        )
    assert len(db.calls)==1
    assert "pending_open_ts" in db.calls[0][0]
