"""Offline, no-order regression for observed vs unproven live capabilities."""
from app import t100_web_pilot_live_evidence_matrix_cli as matrix


def fourth():
    return {
        "status": matrix.FOURTH_STATUS,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_bookended_flat_verified": True,
        "fourth_entry_filled_verified": True,
        "fourth_amendment_ack_journaled": True,
        "manual_market_close_exactly_verified": True,
        "finished_stop_exact_identity_verified": True,
        "finished_stop_price_matches_amended_target": True,
        "finished_stop_canceled_not_triggered": True,
        "live_authorization": False,
        "mexc_orders_sent": 0,
    }


def fifth():
    return {
        "status": matrix.FIFTH_STATUS,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_bookended_flat_verified": True,
        "entry_exact_filled": True,
        "one_attached_finished_stop_row": True,
        "stop_order_terminal_executed": True,
        "stop_price_matches_fifth_journal": True,
        "exchange_generated_close_id_present": True,
        "generated_close_exact_full_market_order": True,
        "generated_close_full_quantity_deals_verified": True,
        "historical_stop_error_code": "ZERO",
        "historical_trigger_side": "TP",
        "manual_close_attempted_in_fifth_journal": False,
        "native_attached_exit_execution_correlated": True,
        "native_sl_trigger_certified": False,
        "historical_stop_loss_type": "MISSING_OR_UNKNOWN",
        "no_take_profit_price_observed": True,
        "live_authorization": False,
        "mexc_orders_sent": 0,
    }


def test_verified_live_native_exit_not_misrepresented_as_sl_certification():
    r = matrix.evaluate_exchange_observations(fourth(), fifth())
    assert r["status"].startswith(
        "FOURTH_AND_FIFTH_LIVE_VENUE_OBSERVATIONS_RECONCILED"
    )
    assert r["one_supervised_native_stop_price_edit_observed"]
    assert r["fifth_native_attached_exit_full_market_fill_verified"]
    assert r["fifth_attached_stop_exit_correlated_without_tp_price"]
    assert r["fifth_historical_trigger_side"] == "TP_CONFLICT"
    assert r["native_sl_trigger_strictly_certified"] is False
    assert r["continuous_protection_during_amendment_certified"] is False
    assert r["live_authorization"] is False
    assert r["entry_allowed"] is False
    assert r["mexc_orders_sent"] == 0


def test_incomplete_manual_exit_fails_closed_without_erasing_fifth_evidence():
    f = fourth()
    f["manual_market_close_exactly_verified"] = False
    r = matrix.evaluate_exchange_observations(f, fifth())
    assert r["status"] == "PILOT_EVIDENCE_INCOMPLETE_REVIEW_BEFORE_ADVANCING"
    assert r["one_supervised_native_stop_price_edit_observed"] is False
    assert r["fifth_native_attached_exit_full_market_fill_verified"] is True
    assert r["live_authorization"] is False


def test_manual_fifth_close_cannot_count_as_native_attached_exit():
    f = fifth()
    f["manual_close_attempted_in_fifth_journal"] = True
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["status"] == "PILOT_EVIDENCE_INCOMPLETE_REVIEW_BEFORE_ADVANCING"
    assert r["fifth_native_attached_exit_full_market_fill_verified"] is False
    assert r["native_sl_trigger_strictly_certified"] is False


def test_missing_positive_tp_price_is_not_proof_of_tp_absence():
    f = fifth()
    f["no_take_profit_price_observed"] = False
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["fifth_native_attached_exit_full_market_fill_verified"] is True
    assert r["fifth_attached_stop_exit_correlated_without_tp_price"] is False
    assert r["native_sl_trigger_strictly_certified"] is False
