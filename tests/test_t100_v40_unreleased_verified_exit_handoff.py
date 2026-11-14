"""Read-only integration: signed 1–4 shorts + checkpoints + cursors + SL75."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_bar_cursor import V40BarCursorState
from app.t100_v40_unreleased_exit_policy import V40CompletedBar
from app.t100_v40_unreleased_verified_exit_handoff import (
    V40VerifiedExitHalted,project_verified_owned_v40_exits,
)
from tests.test_t100_v40_four_slot_guardian_policy import evidence

FILL_MS=1_800_001_234
BAR_TS=1_801_800


def bundle(n=4):
    snap,ledger,positions,stops=evidence(n)
    cursors=tuple(V40BarCursorState(
        position_id=row["position_id"],entry_external_oid=row["external_oid"],
        signed_fill_at_ms=FILL_MS,last_committed_open_ts=None,
        pending_open_ts=None,pending_status=None,pending_stop_price=None,
        pending_best_profit_pct=None,pending_trail_armed=None,
        checkpoint_version=row["checkpoint_version"],stop_order_id=row["stop_order_id"],
        confirmed_stop_price=row["stop_price"],best_profit_pct=row["best_profit_pct"],
        trail_armed=row["armed"],cursor_revision=1,
    ) for row in ledger.checkpoints)
    bars={row["symbol"]:V40CompletedBar(
        open_ts=BAR_TS,open=D("100"),high=D("100"),
        low=D("95"),close=D("96"),
    ) for row in ledger.checkpoints}
    return dict(
        snapshot=snap,ledger=ledger,
        signed_account_positions=positions,signed_native_stops=stops,
        cursors=cursors,completed_bars_by_symbol=bars,
        price_ticks_by_symbol={key:D(".01") for key in bars},
        now_ts=BAR_TS+1800,
    )


@pytest.mark.parametrize("n",[1,2,3,4])
def test_complete_source_cannot_grant_live_mutation(n):
    d=bundle(n)
    report=project_verified_owned_v40_exits(**d)
    assert report.protected_position_count==n
    assert report.verified_exchange_and_ledger_bookends
    assert report.verified_durable_bar_cursors
    assert report.batch.unchanged_verified_stop_count==n
    assert report.batch.slots[0].decision.proposed_checkpoint.best_profit_pct==D("5")
    assert not report.live_execution_certified
    assert not report.entries_permitted
    assert not report.stop_amendment_permitted
    assert not report.position_release_permitted
    assert report.exchange_posts==report.database_writes==0


def test_first_live_stop_touched_unverified_not_released_other_slots_survive():
    d=bundle(4)
    first=d["ledger"].checkpoints[0]["symbol"]
    d["completed_bars_by_symbol"][first]=V40CompletedBar(
        open_ts=BAR_TS,open=D("180"),high=D("185"),
        low=D("80"),close=D("100"),
    )
    result=project_verified_owned_v40_exits(**d)
    assert result.native_close_reconciliation_count==1
    assert result.batch.slots[0].decision.model_exit_price==D("180")
    assert result.batch.slots[0].requires_native_order_reconciliation
    assert result.batch.unchanged_verified_stop_count==3
    assert not result.position_release_permitted


def test_plus_14_trail_price_is_pending_not_in_exchange():
    d=bundle(2)
    sym=d["ledger"].checkpoints[0]["symbol"]
    d["completed_bars_by_symbol"][sym]=V40CompletedBar(
        open_ts=BAR_TS,open=D("100"),high=D("100"),
        low=D("86"),close=D("86"),
    )
    out=project_verified_owned_v40_exits(**d)
    assert out.candidate_native_stop_amendment_count==1
    assert out.batch.slots[0].proposed_stop_price==D("86.50")
    assert not out.stop_amendment_permitted


@pytest.mark.parametrize("bad",[
    "missing_cursor","extra_cursor","wrong_oid","wrong_stop","wrong_best",
    "wrong_checkpoint_version","pending","wrong_fill_ms","wrong_position_id",
    "missing_bar","stale_bar","wrong_venue","bad_ledger",
])
def test_account_bookend_cursor_or_causal_gap_halts_entire_batch(bad):
    d=bundle(4)
    cs=list(d["cursors"])
    if bad=="missing_cursor":
        cs=cs[:-1]
    if bad=="extra_cursor":
        cs.append(cs[0])
    if bad=="wrong_oid":
        cs[0]=replace(cs[0],entry_external_oid="fake")
    if bad=="wrong_stop":
        cs[0]=replace(cs[0],confirmed_stop_price=D("90"))
    if bad=="wrong_best":
        cs[0]=replace(cs[0],best_profit_pct=D("6"))
    if bad=="wrong_checkpoint_version":
        cs[0]=replace(cs[0],checkpoint_version=2)
    if bad=="pending":
        cs[0]=replace(cs[0],pending_open_ts=BAR_TS)
    if bad=="wrong_fill_ms":
        cs[0]=replace(cs[0],signed_fill_at_ms=BAR_TS*1000+10)
    if bad=="wrong_position_id":
        cs[0]=replace(cs[0],position_id="999")
    d["cursors"]=tuple(cs)
    if bad=="missing_bar":
        d["completed_bars_by_symbol"].pop(next(iter(d["completed_bars_by_symbol"])))
    if bad=="stale_bar":
        sym=next(iter(d["completed_bars_by_symbol"]))
        d["completed_bars_by_symbol"][sym]=replace(d["completed_bars_by_symbol"][sym],open_ts=BAR_TS-1800)
    if bad=="wrong_venue":
        d["signed_native_stops"]=[]
    if bad=="bad_ledger":
        d["ledger"].checkpoints[0]["stop_price"]=D("99")
    with pytest.raises(V40VerifiedExitHalted):
        project_verified_owned_v40_exits(**d)


def test_mocked_0_slots_never_repurposed_into_exit_or_entry_permit():
    d=bundle(1)
    d["cursors"]=[]
    with pytest.raises(V40VerifiedExitHalted):
        project_verified_owned_v40_exits(**d)


@pytest.mark.parametrize("due",[1,2,3])
def test_due_subset_keeps_four_exchange_native_stops_verified(due):
    d=bundle(4)
    all_symbols=tuple(d["completed_bars_by_symbol"])
    d["completed_bars_by_symbol"]={
        k:v for k,v in d["completed_bars_by_symbol"].items()
        if k in all_symbols[:due]
    }
    d["price_ticks_by_symbol"]={
        k:v for k,v in d["price_ticks_by_symbol"].items()
        if k in all_symbols[:due]
    }
    out=project_verified_owned_v40_exits(**d)
    assert out.protected_position_count==4
    assert len(out.batch.slots)==due
    assert out.batch.unchanged_verified_stop_count==due
    assert {s.symbol for s in out.batch.slots}==set(all_symbols[:due])
    assert out.verified_exchange_and_ledger_bookends
    assert out.verified_durable_bar_cursors
    assert not out.position_release_permitted
    assert out.exchange_posts==out.database_writes==0


def test_one_due_stop_touch_does_not_skip_other_three_protection():
    d=bundle(4)
    first=d["ledger"].checkpoints[0]["symbol"]
    d["completed_bars_by_symbol"]={first:V40CompletedBar(
        open_ts=BAR_TS,open=D("180"),high=D("185"),
        low=D("80"),close=D("100"),
    )}
    d["price_ticks_by_symbol"]={first:D(".01")}
    out=project_verified_owned_v40_exits(**d)
    assert out.protected_position_count==4
    assert len(out.batch.slots)==1
    assert out.batch.native_close_reconciliation_count==1
    assert out.batch.slots[0].requires_native_order_reconciliation
    assert out.position_release_permitted is False


@pytest.mark.parametrize("kind",["none","unknown","extra_tick","missing_tick","unsafe_other_slot"])
def test_due_partition_and_all_account_protection_still_fail_closed(kind):
    d=bundle(4)
    first=next(iter(d["completed_bars_by_symbol"]))
    d["completed_bars_by_symbol"]={
        first:d["completed_bars_by_symbol"][first]
    }
    d["price_ticks_by_symbol"]={first:D(".01")}
    if kind=="none":
        d["completed_bars_by_symbol"]={}
        d["price_ticks_by_symbol"]={}
    if kind=="unknown":
        d["completed_bars_by_symbol"]={"FOREIGN_USDT":d["completed_bars_by_symbol"][first]}
        d["price_ticks_by_symbol"]={"FOREIGN_USDT":D(".01")}
    if kind=="extra_tick":
        d["price_ticks_by_symbol"]["OTHER_USDT"]=D(".01")
    if kind=="missing_tick":
        d["price_ticks_by_symbol"]={}
    if kind=="unsafe_other_slot":
        d["signed_native_stops"][3]["stopLossPrice"]=D("176")
    with pytest.raises(V40VerifiedExitHalted):
        project_verified_owned_v40_exits(**d)
