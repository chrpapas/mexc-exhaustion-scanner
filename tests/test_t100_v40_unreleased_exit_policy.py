"""Offline regression tests: no MEXC calls, DB mutations or live permissions."""
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar, V40ExitCheckpoint, V40ExitPolicyHalted,
    propose_next_completed_bar,
)

ENTRY_TS = 1_800_001
BAR_TS = 1_803_600


def checkpoint(**changes):
    base = dict(
        position_id="123", symbol="EXAMPLE_USDT",
        entry_price=D("100"), filled_at_ts=ENTRY_TS,
        native_stop_price=D("175"), best_profit_pct=D("0"),
        trail_armed=False, last_completed_open_ts=None,
    )
    base.update(changes)
    return V40ExitCheckpoint(**base)


def bar(ts=BAR_TS, *, high="101", low="95", close="96", open_="100"):
    return V40CompletedBar(
        open_ts=ts, high=D(high), low=D(low), close=D(close), open=D(open_),
    )


def review(cp, candle, now=None):
    return propose_next_completed_bar(
        cp, candle, price_tick=D("0.01"),
        now_ts=now if now is not None else candle.open_ts + 1800,
    )


def test_sl75_has_absolute_adverse_first_priority_over_favorable_low():
    outcome = review(checkpoint(), bar(high="175", low="70", close="105"))
    assert outcome.status == "NATIVE_STOP_MAY_HAVE_TRIGGERED_RECONCILE_ONLY"
    assert outcome.requires_venue_exit_reconciliation is True
    assert outcome.exchange_orders_sent == 0
    assert outcome.proposed_checkpoint is None


def test_plus_five_is_not_tp_and_does_not_arm_trailing():
    outcome = review(checkpoint(), bar(low="95", close="96"))
    assert outcome.status == "KEEP_VERIFIED_NATIVE_STOP"
    assert outcome.proposed_checkpoint.trail_armed is False
    assert outcome.proposed_checkpoint.native_stop_price == D("175")
    assert outcome.proposed_checkpoint.best_profit_pct == D("5")


def test_plus_fourteen_arms_and_proposes_gap_only_for_next_bar():
    outcome = review(checkpoint(), bar(high="100", low="86", close="86"))
    assert outcome.status == "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
    assert outcome.requires_exchange_stop_amendment is True
    assert outcome.proposed_native_stop_price == D("86.50")
    assert outcome.proposed_checkpoint.best_profit_pct == D("14")
    assert outcome.proposed_checkpoint.trail_armed is True
    assert outcome.may_persist_without_verified_exchange_readback is False
    assert outcome.live_authorization is False


def test_trailing_uses_monotone_best_and_raises_stop_never():
    first = review(checkpoint(), bar(high="100", low="80", close="80"))
    assert first.proposed_native_stop_price == D("80.50")
    second = review(
        first.proposed_checkpoint,
        bar(BAR_TS+1800, high="80.4", low="81", close="81", open_="80"),
    )
    assert second.status == "KEEP_VERIFIED_NATIVE_STOP"
    assert second.proposed_checkpoint.native_stop_price == D("80.50")
    assert second.proposed_checkpoint.best_profit_pct == D("20")


def test_after_trail_armed_high_touches_preexisting_stop_even_if_low_favorable():
    cp = checkpoint(
        native_stop_price=D("86.50"), trail_armed=True,
        best_profit_pct=D("14"), last_completed_open_ts=BAR_TS,
    )
    result = review(
        cp, bar(BAR_TS+1800, high="86.50", low="72", close="72", open_="85")
    )
    assert result.requires_venue_exit_reconciliation is True
    assert result.model_exit_price == D("86.50")


def test_newly_armed_trail_cannot_exit_within_same_bar():
    result = review(checkpoint(), bar(high="110", low="75", close="75"))
    assert result.requires_venue_exit_reconciliation is False
    assert result.proposed_native_stop_price == D("75.50")


def test_gap_and_partial_pre_entry_bar_fail_closed():
    with pytest.raises(V40ExitPolicyHalted, match="GAP_OR_PRE_ENTRY"):
        review(checkpoint(), bar(BAR_TS + 1800))
    with pytest.raises(V40ExitPolicyHalted, match="GAP_OR_PRE_ENTRY"):
        review(checkpoint(), bar(BAR_TS - 1800))


def test_incomplete_bar_and_invalid_ohlc_fail_closed():
    with pytest.raises(V40ExitPolicyHalted, match="BAR_OR_STOP"):
        review(checkpoint(), bar(), now=BAR_TS+900)
    with pytest.raises(V40ExitPolicyHalted, match="BAR_OR_STOP"):
        review(checkpoint(), bar(high="95", low="99", close="97"))


def test_candidate_stop_crossed_by_current_price_freezes_amendment():
    with pytest.raises(V40ExitPolicyHalted, match="PROPOSED_STOP_ALREADY_CROSSED"):
        review(checkpoint(), bar(low="86", close="87"))


def test_invalid_trail_state_and_price_tick_rejected():
    with pytest.raises(V40ExitPolicyHalted, match="CHECKPOINT_ORIGIN"):
        review(checkpoint(trail_armed=True), bar())
    with pytest.raises(V40ExitPolicyHalted, match="TICK_INVALID"):
        propose_next_completed_bar(
            checkpoint(), bar(), price_tick=D("0"), now_ts=BAR_TS+1800
        )
