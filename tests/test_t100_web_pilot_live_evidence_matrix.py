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
        "operator_ui_trigger_market_sl_confirmed_before_amend": True,
        "supervised_native_stop_full_one_contract_volume_observed": True,
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
    assert r["supervised_native_stop_full_position_quantity_verified"]
    assert r["supervised_trigger_market_sl_confirmed_in_exchange_ui"]
    assert r["supervised_full_position_native_market_sl_observed"]
    assert r["supervised_native_market_type_api_field_verified"] is False
    assert r["fifth_native_attached_exit_full_market_fill_verified"]
    assert r["fifth_attached_stop_exit_correlated_without_tp_price"]
    assert r["fifth_historical_trigger_side"] == "TP_CONFLICT"
    assert r["fifth_historical_trigger_interpretation"] == (
        "PROBABLE_SL_TRIGGER_API_LABEL_CONFLICT"
    )
    assert r["fifth_probable_sl_label_conflict_observed"] is True
    assert r["fifth_historical_tp_label_standalone_release_blocker"] is False
    assert r["fifth_historical_exit_accounting_accepted_despite_label"] is True
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


def test_historical_tp_label_exception_does_not_grant_market_stop_certification():
    r = matrix.evaluate_exchange_observations(fourth(), fifth())
    assert r["fifth_probable_sl_label_conflict_observed"]
    assert not r["fifth_historical_tp_label_standalone_release_blocker"]
    assert r["fifth_explicit_stop_market_type"] == "MISSING_OR_UNKNOWN"
    for key in (
        "native_sl_trigger_strictly_certified",
        "continuous_protection_during_amendment_certified",
        "entire_finished_stop_history_certified",
        "main_t100_restart_recovery_certified_by_these_pilots",
        "main_t100_trailing_stop_certified_by_these_pilots",
        "main_t100_cross_margin_transport_certified_by_these_pilots",
        "main_t100_production_adapter_certified_by_these_pilots",
        "live_authorization",
        "entry_allowed",
    ):
        assert r[key] is False, key


def test_positive_tp_price_keeps_conflict_blocking_historical_exit_accounting():
    f = fifth()
    f["no_take_profit_price_observed"] = False
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["fifth_historical_trigger_side"] == "TP_CONFLICT"
    assert r["fifth_probable_sl_label_conflict_observed"] is False
    assert r["fifth_historical_tp_label_standalone_release_blocker"] is True
    assert r["fifth_historical_exit_accounting_accepted_despite_label"] is False
    assert r["fifth_historical_trigger_interpretation"] == "UNVERIFIED"
    assert r["live_authorization"] is False


def test_explicit_limit_stop_cannot_claim_probable_sl_label_exception():
    f = fifth()
    f["historical_stop_loss_type"] = "LIMIT"
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["fifth_probable_sl_label_conflict_observed"] is False
    assert r["fifth_historical_tp_label_standalone_release_blocker"] is True
    assert r["native_sl_trigger_strictly_certified"] is False


def test_incomplete_native_full_close_never_gets_historical_exception():
    f = fifth()
    f["generated_close_full_quantity_deals_verified"] = False
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["status"] == "PILOT_EVIDENCE_INCOMPLETE_REVIEW_BEFORE_ADVANCING"
    assert r["fifth_historical_trigger_interpretation"] == "UNVERIFIED"
    assert r["fifth_historical_exit_accounting_accepted_despite_label"] is False
    assert r["entry_allowed"] is False


def test_manual_close_fifth_never_gets_label_exception():
    f = fifth()
    f["manual_close_attempted_in_fifth_journal"] = True
    r = matrix.evaluate_exchange_observations(fourth(), f)
    assert r["fifth_probable_sl_label_conflict_observed"] is False
    assert r["fifth_historical_exit_accounting_accepted_despite_label"] is False
    assert r["live_authorization"] is False


def test_bad_fourth_amendment_provenance_blocks_historical_exception():
    f = fourth()
    f["fourth_amendment_ack_journaled"] = False
    r = matrix.evaluate_exchange_observations(f, fifth())
    assert r["fifth_native_attached_exit_full_market_fill_verified"] is True
    assert r["fifth_probable_sl_label_conflict_observed"] is False
    assert r["fifth_historical_exit_accounting_accepted_despite_label"] is False
    assert r["entry_allowed"] is False


def test_ui_confirmed_native_market_stop_is_recorded_without_claiming_unattended_safety():
    result=matrix.evaluate_exchange_observations(fourth(),fifth())
    assert result["supervised_full_position_native_market_sl_observed"] is True
    assert result["supervised_native_market_type_api_field_verified"] is False
    assert result["continuous_protection_during_amendment_certified"] is False
    assert result["main_t100_restart_recovery_certified_by_these_pilots"] is False
    assert result["live_authorization"] is False
    assert result["entry_allowed"] is False


def test_missing_exchange_ui_market_stop_confirmation_does_not_erase_volume_evidence():
    f=fourth()
    f["operator_ui_trigger_market_sl_confirmed_before_amend"]=False
    result=matrix.evaluate_exchange_observations(f,fifth())
    assert result["fourth_pilot_exact_manual_market_close_verified"] is True
    assert result["supervised_native_stop_full_position_quantity_verified"] is True
    assert result["supervised_trigger_market_sl_confirmed_in_exchange_ui"] is False
    assert result["supervised_full_position_native_market_sl_observed"] is False
    assert result["live_authorization"] is False


def test_missing_full_stop_quantity_evidence_does_not_infer_market_stop_from_ui():
    f=fourth()
    f["supervised_native_stop_full_one_contract_volume_observed"]=False
    result=matrix.evaluate_exchange_observations(f,fifth())
    assert result["supervised_native_stop_full_position_quantity_verified"] is False
    assert result["supervised_full_position_native_market_sl_observed"] is False
    assert result["live_authorization"] is False
