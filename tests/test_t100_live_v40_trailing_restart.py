"""Deterministic V40 restart tests: durable floor, revision chain, no reset."""
from __future__ import annotations

from dataclasses import asdict,replace
from decimal import Decimal as D

import pytest

from app.t100_live_v40_trailing_restart import (
    V40TrailingRestartHalted,reconstruct_v40_trailing_after_restart,
)
from tests.test_t100_live_checkpoint_journal import cp


def state(*,armed=False,best="0",stop="175",version=1):
    record=asdict(cp(
        armed=armed,best_profit_pct=D(best),
        stop_price=D(stop),checkpoint_version=version,
    ))
    first=asdict(cp())
    first["write_reason"]="ENTRY_PROTECTED"
    revisions=[first]
    if version>1:
        second=dict(record,write_reason="STOP_VERIFIED")
        revisions.append(second)
    return record,revisions


def resume(*,row=None,revisions=None,low=None,fill=False,stream=False):
    if row is None or revisions is None:
        row,revisions=state()
    return reconstruct_v40_trailing_after_restart(
        checkpoint_row=row,revision_rows=revisions,
        observed_post_entry_low=low,price_tick=D(".01"),
        signed_fill_time_verified=fill,
        full_post_fill_low_stream_verified=stream,
    )


def test_new_position_with_no_tick_history_preserves_stop_and_unarmed():
    out=resume()
    assert out.status=="V40_TRAILING_RESTART_DURABLE_FLOOR_ONLY_FREEZE"
    assert out.next_best_profit_pct==D(0)
    assert out.next_armed is False
    assert out.proposed_stop_price==D(175)
    assert not out.proposed_stop_tightening
    assert out.durable_revision_history_verified
    assert not out.stop_amendment_authorized
    assert not out.live_authorization


def test_incomplete_low_cannot_arm_or_modify_native_stop_even_if_low_favorable():
    out=resume(low=D("80"))
    assert out.recovered_best_profit_pct==D(0)
    assert out.next_best_profit_pct==D(0)
    assert not out.next_armed
    assert out.proposed_stop_price==D(175)
    assert not out.proposed_stop_tightening


def test_full_signed_low_proposes_frozen_14_percent_0p5_gap_without_post():
    out=resume(low=D(86),fill=True,stream=True)
    assert out.status=="V40_TRAILING_RESTART_CANDIDATE_REBUILT_NONAUTHORIZING"
    assert out.next_best_profit_pct==D(14)
    assert out.next_armed
    assert out.proposed_stop_price==D("86.5")
    assert out.proposed_stop_tightening
    assert out.post_fill_low_stream_complete
    assert out.stop_amendment_authorized is False


def test_recovery_never_disarms_or_loosens_after_rebound():
    row,revisions=state(armed=True,best="18",stop="82.5",version=2)
    out=resume(row=row,revisions=revisions,low=D(97),fill=True,stream=True)
    assert out.recovered_best_profit_pct==D(18)
    assert out.next_best_profit_pct==D(18)
    assert out.next_armed is True
    assert out.proposed_stop_price==D("82.5")
    assert not out.proposed_stop_tightening


def test_missing_live_low_does_not_wipe_durable_armed_state():
    row,revisions=state(armed=True,best="18",stop="82.5",version=2)
    out=resume(row=row,revisions=revisions)
    assert out.next_armed and out.next_best_profit_pct==D(18)
    assert out.proposed_stop_price==D("82.5")
    assert not out.post_fill_low_stream_complete


def test_signed_fill_without_stream_is_not_full_low_provenance():
    out=resume(low=D(85),fill=True,stream=False)
    assert out.status=="V40_TRAILING_RESTART_DURABLE_FLOOR_ONLY_FREEZE"
    assert out.proposed_stop_price==D(175)


def test_caller_cannot_claim_complete_low_history_without_signed_entry():
    with pytest.raises(V40TrailingRestartHalted,match="WITHOUT_SIGNED_FILL"):
        resume(low=D(85),fill=False,stream=True)


@pytest.mark.parametrize("mutation",[
    "missing_revision","wrong_revision_reason",
    "best_regression","stop_loosening","final_revision_mismatch",
    "extra_revision","disarm","version_gap",
])
def test_revision_inconsistency_halts_without_synthetic_restart_fix(mutation):
    row,revisions=state(armed=True,best="18",stop="82.5",version=2)
    if mutation=="missing_revision":
        revisions=revisions[:1]
    if mutation=="wrong_revision_reason":
        revisions[1]["write_reason"]="ENTRY_PROTECTED"
    if mutation=="best_regression":
        revisions[0]["best_profit_pct"]=D("20")
        revisions[0]["armed"]=True
    if mutation=="stop_loosening":
        revisions[0]["stop_price"]=D(80)
    if mutation=="final_revision_mismatch":
        row["best_profit_pct"]=D(19)
        row["armed"]=True
    if mutation=="extra_revision":
        revisions.append(dict(revisions[1]))
    if mutation=="disarm":
        revisions[0]["armed"]=True
        revisions[0]["best_profit_pct"]=D(14)
    if mutation=="version_gap":
        revisions[1]["checkpoint_version"]=3
    with pytest.raises(V40TrailingRestartHalted,match="DURABLE_HISTORY"):
        resume(row=row,revisions=revisions,low=D(70),fill=True,stream=True)


def test_unknown_nan_and_zero_low_rejected_before_mathematics():
    row,revisions=state()
    for low in (D("NaN"),D(0),D("Infinity"),D(-5),"-100"):
        with pytest.raises(V40TrailingRestartHalted):
            resume(row=row,revisions=revisions,low=low,fill=True,stream=True)


def test_preexisting_stop_is_never_replaced_by_unverified_low():
    row,revisions=state(armed=True,best="14",stop="86.5",version=2)
    out=resume(row=row,revisions=revisions,low=D(50),fill=False,stream=False)
    assert out.next_best_profit_pct==D(14)
    assert out.proposed_stop_price==D("86.5")
    assert not out.proposed_stop_tightening
