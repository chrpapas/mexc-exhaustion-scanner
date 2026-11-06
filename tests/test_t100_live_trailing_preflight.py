"""Offline T100/V40 trailing proposal gate; no venue access or orders."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_trailing_preflight import (
    TrailingBookend,
    review_t100_trailing_candidate,
)

NOW = 1780000000000


def checkpoint(symbol="BTC_USDT", pid="77", sid="811"):
    return PositionCheckpoint(
        symbol=symbol,
        external_oid="test-owned-77",
        exchange_order_id="66",
        position_id=pid,
        contracts=D("1"),
        entry_price=D("100"),
        stop_order_id=sid,
        stop_price=D("175"),
        best_profit_pct=D("0"),
        armed=False,
        lifecycle_state="OPEN",
        checkpoint_version=1,
    )


def pos(cp):
    return {
        "positionId": cp.position_id, "symbol": cp.symbol,
        "positionType": 2, "openType": 1, "state": 1,
        "holdVol": str(cp.contracts), "leverage": 1,
    }


def stop(cp):
    return {
        "id": cp.stop_order_id, "orderId": 0,
        "positionId": cp.position_id, "symbol": cp.symbol,
        "state": 1, "isFinished": 0,
        "positionType": 2, "triggerSide": 0,
        "errorCode": 0, "stopLossType": 0,
        "stopLossReverse": 2, "lossTrend": 1,
        "volType": 2, "profitLossVolType": "SAME",
        "stopLossPrice": str(cp.stop_price),
    }


def report(n):
    return {
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": n, "after_positions": n,
        "before_current_tpsl": n, "after_current_tpsl": n,
        "open_order_counts": {
            "sumCount": n, "stopOrderCount": n, "limitOrderCount": 0,
            "planOrderCount": 0, "trackOrderCount": 0,
        },
    }


def book(checkpoints, *, timestamp=NOW-2000):
    n = len(checkpoints)
    return TrailingBookend(
        positions=[pos(cp) for cp in checkpoints],
        stops=[stop(cp) for cp in checkpoints],
        completeness=report(n),
        observed_at_ms=timestamp,
    )


def inspect(*, cps=None, first=None, second=None, **overrides):
    cps = [checkpoint()] if cps is None else cps
    initial = book(cps)
    values = dict(
        checkpoints=cps,
        target_position_id=cps[0].position_id,
        first=initial if first is None else first,
        second=replace(initial, observed_at_ms=NOW-1000) if second is None else second,
        observed_low_since_entry=D("86"),
        current_last_price=D("86.1"),
        price_tick=D("0.01"),
        market_observed_at_ms=NOW-500,
        now_ms=NOW,
    )
    values.update(overrides)
    return review_t100_trailing_candidate(**values)


def test_frozen_14_percent_activation_and_no_authorization():
    result = inspect()
    assert result.status == "TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
    assert result.proposal.proposed_stop == D("86.5")
    assert result.proposal.old_stop == D("175")
    assert result.proposal.best_profit_pct == D("14")
    assert result.proposal.armed is True
    assert result.all_owned_full_volume_market_stops_observed
    assert result.account_bookends_equal
    assert result.frozen_trailing_policy_matched
    assert not result.market_low_provenance_verified
    assert not result.signed_get_provenance_independently_verified
    assert not result.live_stop_mutation_allowed
    assert not result.live_authorization
    assert not result.mutation_attempted


def test_no_trailing_before_14_percent():
    r = inspect(observed_low_since_entry=D("90"), current_last_price=D("91"))
    assert r.status == "TRAILING_NO_STOP_UPDATE_DUE"
    assert r.proposal is None


def test_rebound_crossing_target_does_not_propose_unprotected_stop():
    r = inspect(current_last_price=D("100"))
    assert r.status == "TRAILING_PROPOSED_STOP_ALREADY_CROSSED"
    assert r.proposal is None


def test_running_best_and_monotonic_stop():
    cp = replace(checkpoint(), stop_price=D("86.50"),
                 best_profit_pct=D("14"), armed=True, checkpoint_version=2)
    r = inspect(cps=[cp], observed_low_since_entry=D("80"),
                current_last_price=D("80.1"))
    assert r.proposal.proposed_stop == D("80.5")
    assert r.proposal.old_stop == D("86.50")
    assert r.proposal.best_profit_pct == D("20")


def test_two_owned_positions_and_full_inventory():
    a, b = checkpoint(), checkpoint(symbol="ETH_USDT", pid="78", sid="812")
    r = inspect(cps=[a, b])
    assert r.status == "TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
    assert r.proposal.position_id == "77"


def test_four_slots_supported_without_adopting_any_manual_position():
    cps = [checkpoint(symbol=f"COIN{i}_USDT", pid=str(77+i),
                      sid=str(811+i)) for i in range(4)]
    r = inspect(cps=cps)
    assert r.status == "TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
    more = cps + [checkpoint(symbol="EXTRA_USDT", pid="90", sid="900")]
    assert inspect(cps=more).status == "TRAILING_INPUT_OR_TIMESTAMP_INVALID"


def test_manual_extra_position_blocks():
    cp = checkpoint()
    initial = book([cp])
    initial.positions.append(pos(checkpoint("ETH_USDT", "78", "812")))
    initial = replace(initial, completeness=report(2))
    second = replace(initial, observed_at_ms=NOW-1000)
    assert inspect(first=initial, second=second).status == (
        "TRAILING_UNOWNED_INVENTORY_PRESENT"
    )


@pytest.mark.parametrize("field,value,expected", [
    ("orderId", "222", "NATIVE_SL_POSITION_ORDER_ORIGIN_UNVERIFIED"),
    ("stopLossType", 1, "NATIVE_SL_MARKET_TRIGGER_OR_REVERSE_UNVERIFIED"),
    ("stopLossReverse", 1, "NATIVE_SL_MARKET_TRIGGER_OR_REVERSE_UNVERIFIED"),
    ("triggerSide", 2, "NATIVE_SL_TRIGGER_OR_EXECUTION_ERROR_UNVERIFIED"),
    ("errorCode", 12, "NATIVE_SL_TRIGGER_OR_EXECUTION_ERROR_UNVERIFIED"),
    ("volType", 0, "NATIVE_SL_FULL_CONTRACT_COVERAGE_UNVERIFIED"),
    ("stopLossPrice", "177", "NATIVE_SL_PRICE_NOT_PROTECTIVE"),
    ("positionId", "12345", "TRAILING_POSITION_STOP_OWNERSHIP_MISMATCH"),
])
def test_bad_native_stop_fails_closed(field, value, expected):
    cp = checkpoint()
    b = book([cp])
    b.stops[0][field] = value
    s = replace(b, observed_at_ms=NOW-1000)
    r = inspect(first=b, second=s)
    assert expected in r.status
    assert not r.live_authorization


def test_mismatched_bookends_block():
    cp = checkpoint()
    a = book([cp])
    b = book([cp], timestamp=NOW-1000)
    b.stops[0]["stopLossPrice"] = "170"
    assert inspect(first=a, second=b).status == "TRAILING_CRITICAL_ACCOUNT_DRIFT"


def test_tpsl_count_mismatch_blocks():
    cp = checkpoint()
    b = book([cp])
    b.completeness["open_order_counts"]["stopOrderCount"] = 2
    assert inspect(first=b, second=b).status == (
        "TRAILING_ACCOUNT_COMPLETENESS_UNVERIFIED"
    )


def test_unrelated_open_limit_order_blocks():
    cp = checkpoint()
    b = book([cp])
    b.completeness["open_order_counts"].update(
        sumCount=2, limitOrderCount=1,
    )
    assert inspect(first=b, second=b).status == (
        "TRAILING_ACCOUNT_COMPLETENESS_UNVERIFIED"
    )


def test_missing_checkpoint_ownership_blocks():
    cp = checkpoint()
    assert inspect(cps=[cp], target_position_id="999").status == (
        "TRAILING_OWNERSHIP_OR_SLOT_AMBIGUOUS"
    )


def test_duplicate_symbol_in_checkpoint_blocks():
    cp = checkpoint()
    other = replace(cp, position_id="78", stop_order_id="812")
    assert inspect(cps=[cp, other]).status == (
        "TRAILING_OWNERSHIP_OR_SLOT_AMBIGUOUS"
    )


def test_stale_or_future_timestamp_blocks():
    assert inspect(market_observed_at_ms=NOW-11000).status == (
        "TRAILING_BOOKEND_OR_MARKET_STALE"
    )
    assert inspect(market_observed_at_ms=NOW+3000).status == (
        "TRAILING_BOOKEND_OR_MARKET_STALE"
    )


def test_low_cannot_be_above_latest_price():
    assert inspect(observed_low_since_entry=D("90")).status == (
        "TRAILING_LOW_INCONSISTENT_WITH_MARKET"
    )


def test_arm_flag_best_profit_incoherence_blocks():
    cp = replace(checkpoint(), armed=True)
    assert inspect(cps=[cp]).status == "TRAILING_DURABLE_CHECKPOINT_INVALID"


def test_nan_or_invalid_market_data_blocks():
    assert inspect(current_last_price=D("NaN")).status == (
        "TRAILING_INPUT_OR_TIMESTAMP_INVALID"
    )
    assert inspect(price_tick=D("-0.01")).status == (
        "TRAILING_INPUT_OR_TIMESTAMP_INVALID"
    )
