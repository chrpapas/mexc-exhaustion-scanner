"""Frozen priority after owned symbols are excluded; no exchange or DB access.

These tests prove second-slot selection does NOT silently skip an earlier
unheld same-bar candidate or reuse a stale monitored candidate.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_stage2_source import V40SourceBatch
from app.t100_live_v40_entry_coordinator import (
    V40CoordinatorHalted, select_frozen_portfolio_pinned_candidate,
)
from tests.test_t100_live_v40_entry_coordinator import (
    candidate,snapshot,controller,caps,
    _portfolio_permit_for,patch_release_gates_and_mutations,
)


def queue(*,reverse=False):
    first=candidate()
    second=replace(first,signal_id=43,symbol="SECOND_USDT")
    third=replace(first,signal_id=44,symbol="THIRD_USDT")
    ordered=(first,second,third)
    return V40SourceBatch(
        "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),candidates=ordered[::-1] if reverse else ordered,
    )


def owned(*symbols):
    return FourSlotSnapshotResult(
        status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=len(symbols),
        recovery=SimpleNamespace(recovered=tuple(
            SimpleNamespace(position_id=f"slot-{i}",symbol=symbol)
            for i,symbol in enumerate(symbols)
        )),
    )


def pinned(tape,frame,candidate_index):
    x=tape.candidates[candidate_index]
    return select_frozen_portfolio_pinned_candidate(
        tape,frame,signal_id=x.signal_id,symbol=x.symbol,tier=x.tier,
    )


def test_flat_first_priority_is_preserved():
    tape=queue()
    chosen=pinned(tape,snapshot(),0)
    assert chosen.signal_id==42
    with pytest.raises(V40CoordinatorHalted,match="NOT_FIRST_UNHELD"):
        pinned(tape,snapshot(),1)


def test_first_owned_second_is_only_valid_portfolio_pinned_signal():
    tape=queue()
    current=owned("EXAMPLE_USDT")
    assert pinned(tape,current,1).signal_id==43
    with pytest.raises(V40CoordinatorHalted,match="NOT_FIRST_UNHELD"):
        pinned(tape,current,0)
    with pytest.raises(V40CoordinatorHalted,match="NOT_FIRST_UNHELD"):
        pinned(tape,current,2)


def test_two_owned_symbols_admit_third_as_next_frozen_signal():
    tape=queue()
    assert pinned(tape,owned("EXAMPLE_USDT","SECOND_USDT"),2).signal_id==44


def test_unrelated_owned_symbol_cannot_displace_first_candidate():
    tape=queue()
    with pytest.raises(V40CoordinatorHalted,match="NOT_FIRST_UNHELD"):
        pinned(tape,owned("FOURTH_USDT"),1)


def test_all_eligible_symbols_owned_means_no_automatic_trade():
    tape=queue()
    with pytest.raises(V40CoordinatorHalted,match="ALL_STAGE2_SYMBOLS_HELD"):
        pinned(tape,owned("EXAMPLE_USDT","SECOND_USDT","THIRD_USDT"),0)


@pytest.mark.parametrize("corruption",[
    "reordered","duplicate_symbol","duplicate_id","wrong_slot_count",
    "duplicate_held","missing_recovery","unvalidated_snapshot",
    "already_four_slots",
])
def test_unverified_queue_or_owned_topology_halts(corruption):
    tape=queue()
    frame=owned("EXAMPLE_USDT")
    if corruption=="reordered":
        tape=queue(reverse=True)
    elif corruption=="duplicate_symbol":
        tape=replace(tape,candidates=(
            tape.candidates[0],replace(tape.candidates[1],symbol="EXAMPLE_USDT"),
            tape.candidates[2],
        ))
    elif corruption=="duplicate_id":
        tape=replace(tape,candidates=(
            tape.candidates[0],replace(tape.candidates[1],signal_id=42),
            tape.candidates[2],
        ))
    elif corruption=="wrong_slot_count":
        frame=replace(frame,occupied_slots=2)
    elif corruption=="duplicate_held":
        frame=owned("EXAMPLE_USDT","EXAMPLE_USDT")
    elif corruption=="missing_recovery":
        frame=replace(frame,recovery=None)
    elif corruption=="unvalidated_snapshot":
        frame=replace(frame,status="UNKNOWN")
    elif corruption=="already_four_slots":
        frame=owned("EXAMPLE_USDT","SECOND_USDT","THIRD_USDT","FOURTH_USDT")
    with pytest.raises(V40CoordinatorHalted):
        select_frozen_portfolio_pinned_candidate(
            tape,frame,signal_id=43,symbol="SECOND_USDT",tier="LOW_100",
        )


def test_nonflat_second_slot_coordinator_calls_planner_for_exact_second_only(monkeypatch):
    import app.t100_live_v40_entry_coordinator as m
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    tape=queue()
    first,second=tape.candidates[:2]
    c=controller(log,events=[first,second])
    class IndependentNonflatSnapshot:
        async def inspect(self):
            log.append("fresh_nonflat_snapshot")
            return owned(first.symbol)
    c.snapshots=IndependentNonflatSnapshot()
    class Witness:
        async def inspect(self,signal):
            log.append("risk_for_"+str(signal.signal_id))
            return object(),object()
    c.witness=Witness()
    planned=[]
    def deny_without_mutation(*,signal,**kwargs):
        planned.append(signal.signal_id)
        return None
    monkeypatch.setattr(m,"prepare_candidate",deny_without_mutation)
    result=asyncio.run(c.run_one(
        permit=_portfolio_permit_for(second),
        caps=caps(),parity_report={},
        holder_id="test-second-slot-frozen",
        monitored_signal_id=second.signal_id,
        monitored_symbol=second.symbol,monitored_tier=second.tier,
    ))
    assert result.status=="V40_NO_ADMISSIBLE_STAGE2_CANDIDATE"
    assert planned==[43]
    assert "risk_for_43" in log and "risk_for_42" not in log
    assert "reserve" not in log and "stage" not in log
    assert "dispatch" not in log
    assert "lease_release" in log
