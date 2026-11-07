"""Offline-only completed T100 web pilot finished-TP/SL audit tests."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from app.t100_web_pilot_finished_stop_audit_cli import (
    FinishedStopForensicsHalted,
    FinishedStopGetOnly,
    assess_closed_pilot_finished_stop,
    audit,
)

OID = "TPWEB" + "a" * 24
CLOSE_OID = "TPWEB" + "b" * 24


def journal():
    return {
        "version": 1, "symbol": "BTC_USDT",
        "entry_post_attempted": True, "entry_ack_verified": True,
        "close_post_attempted": True, "close_ack_verified": True,
        "external_oid": OID, "close_oid": CLOSE_OID,
        "draft": {
            "externalOid": OID,
            "stopLossPrice": "76680.1",
        },
    }


def entry():
    return {
        "orderId": "123456", "positionId": "87654",
        "externalOid": OID, "symbol": "BTC_USDT",
        "side": 3, "state": 3, "openType": 1,
        "dealVol": 1, "vol": 1,
    }


def close():
    return {
        "orderId": "123457", "positionId": "87654",
        "externalOid": CLOSE_OID, "symbol": "BTC_USDT",
        "side": 2, "state": 3, "dealVol": 1, "vol": 1,
    }


def stop():
    return {
        "id": "789", "orderId": "123456", "positionId": "87654",
        "symbol": "BTC_USDT", "positionType": 2,
        "lossTrend": 1, "stopLossPrice": "76680.1",
        "isFinished": 1, "state": 2, "triggerSide": 0,
        "stopLossType": 0, "stopLossReverse": 2,
        "volType": 2, "profitLossVolType": "SAME", "errorCode": 0,
    }


def flat():
    return {
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": 0, "after_positions": 0,
        "before_current_tpsl": 0, "after_current_tpsl": 0,
        "reported_open_order_counts": {
            "sumCount": 0, "limitOrderCount": 0, "stopOrderCount": 0,
            "planOrderCount": 0, "trackOrderCount": 0,
        },
    }


def assess(**overrides):
    kwargs = dict(
        journal=journal(), entry_order=entry(), close_order=close(),
        finished_rows=[stop()], flat_inventory=flat(),
        pages_read=1,
    )
    kwargs.update(overrides)
    return assess_closed_pilot_finished_stop(**kwargs)


def test_exact_embedded_stop_terminal_canceled_not_triggered():
    r = assess()
    assert r["status"] == "ORDER_ATTACHED_STOP_TERMINAL_UNTRIGGERED_OBSERVED"
    assert r["attached_entry_stop_terminal_observed"]
    assert r["entry_filled_verified"]
    assert r["close_filled_verified"]
    assert r["account_bookend_flat_verified"]
    assert r["terminal_state"] == "CANCELED"
    assert r["trigger_side"] == "NOT_TRIGGERED"
    assert r["market_stop_type_observed"]
    assert r["full_position_mode_observed"]
    assert not r["finished_stop_history_complete"]
    assert not r["native_stop_triggered_verified"]
    assert not r["native_stop_execution_certified"]
    assert not r["live_authorization"]


@pytest.mark.parametrize("field,expected_key,expected_status", [
    ("stopLossType", "stop_loss_type_status", "NOT_REPORTED"),
    ("volType", "volume_type_status", "NOT_REPORTED"),
    ("profitLossVolType", "profit_loss_volume_mode_status", "NOT_REPORTED"),
    ("errorCode", "error_code_status", "NOT_REPORTED"),
])
def test_missing_historical_fields_are_unknown_not_negative_evidence(
    field, expected_key, expected_status,
):
    s = stop()
    del s[field]
    result = assess(finished_rows=[s])
    assert result["attached_entry_stop_terminal_observed"]
    assert result[expected_key] == expected_status
    assert not result["native_stop_execution_certified"]
    assert not result["live_authorization"]


def test_explicit_limit_and_partial_metadata_distinguished_from_absent():
    s = stop()
    s["stopLossType"] = 1
    s["volType"] = 1
    s["profitLossVolType"] = "SEPARATE"
    s["errorCode"] = 1234
    result = assess(finished_rows=[s])
    assert result["status"] == "ORDER_ATTACHED_STOP_TERMINAL_UNTRIGGERED_OBSERVED"
    assert result["stop_loss_type_status"] == "LIMIT"
    assert result["volume_type_status"] == "PARTIAL"
    assert result["profit_loss_volume_mode_status"] == "SEPARATE"
    assert result["error_code_status"] == "NONZERO_REPORTED"
    assert not result["market_stop_type_observed"]
    assert not result["full_position_mode_observed"]
    assert not result["no_error_reported"]


@pytest.mark.parametrize("field,status_key", [
    ("stopLossType", "stop_loss_type_status"),
    ("volType", "volume_type_status"),
    ("errorCode", "error_code_status"),
    ("stopLossReverse", "stop_loss_reverse_status"),
])
def test_boolean_venue_numeric_fields_never_validate_as_zero(field, status_key):
    s = stop()
    s[field] = False
    result = assess(finished_rows=[s])
    assert result[status_key] == "UNEXPECTED_TYPE"
    assert not result["native_stop_execution_certified"]


def test_original_fixture_reports_explicit_verified_fields():
    result = assess()
    assert result["stop_loss_type_status"] == "MARKET"
    assert result["volume_type_status"] == "ENTIRE_POSITION"
    assert result["profit_loss_volume_mode_status"] == "SAME"
    assert result["error_code_status"] == "ZERO_NORMAL"
    assert result["stop_loss_reverse_status"] == "NO_REVERSE"


def test_documented_auto_cancel_code_maps_without_claiming_stop_executed():
    s = stop()
    s["errorCode"] = 8912
    s["stopLossVol"] = 1
    result = assess(finished_rows=[s])
    assert result["error_code_status"] == "NONZERO_REPORTED"
    assert result["error_code_value"] == 8912
    assert result["error_code_documented_meaning"] == (
        "AUTOMATICALLY_CANCELED_AS_POSITION_CLOSED"
    )
    assert result["historical_stop_loss_quantity_check"] == (
        "SL_QUANTITY_MATCHES_ENTRY_FILL_HISTORICALLY"
    )
    assert result["historical_stop_loss_volume_equal_one_contract"]
    assert not result["live_stop_full_coverage_certified"]
    assert not result["native_stop_execution_certified"]
    assert not result["live_authorization"]


@pytest.mark.parametrize("quantity,expected", [
    (None, "SL_QUANTITY_MISSING_OR_NONPOSITIVE"),
    ("0", "SL_QUANTITY_MISSING_OR_NONPOSITIVE"),
    ("0.5", "SL_QUANTITY_BELOW_ENTRY_FILL"),
    ("1", "SL_QUANTITY_MATCHES_ENTRY_FILL_HISTORICALLY"),
    ("2", "SL_QUANTITY_EXCEEDS_ENTRY_FILL"),
    ("not-a-number", "SL_QUANTITY_MISSING_OR_NONPOSITIVE"),
])
def test_separate_stop_loss_quantity_uses_stop_loss_vol_not_vol(
    quantity, expected,
):
    s = stop()
    s["vol"] = 1
    s["stopLossVol"] = quantity
    result = assess(finished_rows=[s])
    assert result["historical_stop_loss_quantity_check"] == expected
    assert not result["live_stop_full_coverage_certified"]


def test_fully_attached_stop_history_may_omit_market_stop_type():
    s = stop()
    s.pop("stopLossType")
    s["stopLossVol"] = 1
    s["errorCode"] = 8912
    result = assess(finished_rows=[s])
    assert result["stop_loss_type_status"] == "NOT_REPORTED"
    assert result["historical_stop_loss_quantity_check"] == (
        "SL_QUANTITY_MATCHES_ENTRY_FILL_HISTORICALLY"
    )
    assert not result["market_stop_type_observed"]
    assert not result["native_stop_execution_certified"]


def test_unknown_nonzero_error_does_not_infer_normal_auto_cancel():
    s = stop()
    s["errorCode"] = 5931
    result = assess(finished_rows=[s])
    assert result["error_code_value"] == 5931
    assert result["error_code_documented_meaning"] == "UNKNOWN_OR_NOT_REPORTED"
    assert not result["no_error_reported"]


def test_untrusted_boolean_error_code_rejected_without_code_number():
    s = stop()
    s["errorCode"] = False
    result = assess(finished_rows=[s])
    assert result["error_code_status"] == "UNEXPECTED_TYPE"
    assert result["error_code_value"] is None
    assert result["error_code_documented_meaning"] == "UNKNOWN_OR_NOT_REPORTED"


def test_take_profit_presence_is_diagnostic_not_proof_of_execution():
    s = stop()
    s["takeProfitPrice"] = "75000"
    result = assess(finished_rows=[s])
    assert result["take_profit_price_status"] == "POSITIVE_REPORTED"
    assert result["trigger_side"] == "NOT_TRIGGERED"
    assert not result["native_stop_triggered_verified"]


def test_invalidated_untriggered_also_only_terminal_observation():
    s = stop()
    s["state"] = 4
    r = assess(finished_rows=[s])
    assert r["status"] == "ORDER_ATTACHED_STOP_TERMINAL_UNTRIGGERED_OBSERVED"
    assert r["terminal_state"] == "INVALIDATED"


def test_triggered_stop_cannot_certify_execution():
    s = stop()
    s["state"], s["triggerSide"] = 3, 2
    r = assess(finished_rows=[s])
    assert r["status"] == "STOP_TRIGGER_ROW_SEEN_BUT_EXECUTION_NOT_CERTIFIED"
    assert not r["native_stop_execution_certified"]


def test_stop_execution_failed_is_reported():
    s = stop()
    s["state"], s["triggerSide"] = 5, 2
    r = assess(finished_rows=[s])
    assert r["status"] == "STOP_EXECUTION_FAILURE_REPORTED"
    assert not r["native_stop_execution_certified"]


@pytest.mark.parametrize("field,value", [
    ("orderId", "999"), ("positionId", "999"),
    ("symbol", "ETH_USDT"), ("stopLossPrice", "76800"),
    ("lossTrend", 2), ("isFinished", 0),
    ("state", 1), ("triggerSide", -1),
    ("positionType", 1),
])
def test_mismatched_stop_never_claimed_evidence(field, value):
    s = stop()
    s[field] = value
    r = assess(finished_rows=[s])
    assert not r["attached_entry_stop_terminal_observed"]
    assert not r["native_stop_execution_certified"]


@pytest.mark.parametrize("field,value", [
    ("externalOid", "WRONG"), ("side", 1), ("dealVol", 0),
    ("vol", 2), ("state", 2), ("symbol", "ETH_USDT"),
])
def test_entry_or_close_not_filled_blocks(field, value):
    o = entry()
    o[field] = value
    r = assess(entry_order=o)
    assert r["status"] == "PILOT_ENTRY_CLOSE_OR_FLATNESS_UNVERIFIED"
    assert not r["attached_entry_stop_terminal_observed"]


def test_unmatched_history_is_not_proof_of_absence():
    r = assess(finished_rows=[])
    assert r["status"] == "FINISHED_EMBEDDED_STOP_NOT_FOUND_IN_SCOPED_PAGES"
    assert not r["finished_stop_history_complete"]
    assert not r["native_stop_execution_certified"]


def test_duplicate_linked_history_is_ambiguous():
    r = assess(finished_rows=[stop(), stop()])
    assert r["status"] == "FINISHED_EMBEDDED_STOP_AMBIGUOUS"
    assert not r["attached_entry_stop_terminal_observed"]


def test_flat_bookend_failure_blocks():
    f = flat()
    f["after_positions"] = 1
    r = assess(flat_inventory=f)
    assert r["status"] == "PILOT_ENTRY_CLOSE_OR_FLATNESS_UNVERIFIED"
    assert not r["account_bookend_flat_verified"]


def test_close_oid_mismatch_or_unacknowledged_blocks():
    j = journal()
    j["close_oid"] = OID
    assert assess(journal=j)["status"] == "PILOT_JOURNAL_OR_VENUE_RESPONSE_UNVERIFIED"
    j["close_oid"] = CLOSE_OID
    j["close_ack_verified"] = False
    assert assess(journal=j)["status"] == "PILOT_JOURNAL_OR_VENUE_RESPONSE_UNVERIFIED"


def fake():
    return type("Client", (), {
        "_private_get": AsyncMock(return_value=[]),
        "_private_post": AsyncMock(),
    })()


@pytest.mark.asyncio
async def test_get_only_allowlist_and_no_post_method():
    client = fake()
    safe = FinishedStopGetOnly(client)
    assert not hasattr(safe, "_private_post")
    with pytest.raises(FinishedStopForensicsHalted):
        await safe.get_order("OTHER")
    with pytest.raises(FinishedStopForensicsHalted):
        await safe.get_stop_page(0)
    with pytest.raises(FinishedStopForensicsHalted):
        await safe.get_stop_page(4)
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_history_audit_bookends_and_no_post():
    client = fake()
    client._private_get.side_effect = [entry(), close(), [stop()]]
    with patch(
        "app.t100_web_pilot_finished_stop_audit_cli.run_inventory",
        new=AsyncMock(side_effect=[flat(), flat()]),
    ) as inventory:
        report = await audit(client, journal())
    assert report["status"] == "ORDER_ATTACHED_STOP_TERMINAL_UNTRIGGERED_OBSERVED"
    assert report["flat_before_and_after_history_verified"]
    assert inventory.await_count == 2
    assert client._private_get.await_count == 3
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_pages_fail_closed_without_post():
    client = fake()
    page = [stop()]
    page += [dict(stop(), id=str(i + 1000)) for i in range(99)]
    client._private_get.side_effect = [entry(), close(), page, [stop()]]
    with patch(
        "app.t100_web_pilot_finished_stop_audit_cli.run_inventory",
        new=AsyncMock(return_value=flat()),
    ):
        with pytest.raises(FinishedStopForensicsHalted, match="DUPLICATED"):
            await audit(client, journal())
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_failing_account_bookend_never_gives_success():
    client = fake()
    client._private_get.side_effect = [entry(), close(), [stop()]]
    bad = flat()
    bad["after_positions"] = 1
    with patch(
        "app.t100_web_pilot_finished_stop_audit_cli.run_inventory",
        new=AsyncMock(side_effect=[flat(), bad]),
    ):
        with pytest.raises(FinishedStopForensicsHalted, match="FLAT"):
            await audit(client, journal())
    client._private_post.assert_not_awaited()
