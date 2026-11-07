"""GET-only third real pilot's final TP/SL record: strict provenance cases."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest

from app import t100_web_pilot_third_finished_stop_cli as audit
from app import t100_web_pilot_third_close_audit_cli as close_audit
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_tight_stop_trigger_cli as pilot

FP = "a" * 64


def private():
    return {
        "account_fingerprint_sha256": FP,
        "journal_amend_post_attempted": False,
        "entry_contracts": "1",
        "entry_order_id": "101",
        "position_id": "202",
        "close_order_id": "303",
        "close_deals": [{
            "deal_id": "505", "order_id": "303",
            "timestamp_ms": 1794050000000, "qty": "1",
            "fill_price": "75200", "taker": True,
        }],
    }


def good_close():
    return {
        "status": "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING",
        "account_bookended_flat_verified": True,
        "all_historical_orders_complete": False,
        "close_origin": "UNKNOWN_OR_EXTERNAL",
    }


def finished(**overrides):
    record = {
        "id": "404", "orderId": "101",
        "positionId": "202", "placeOrderId": "303",
        "symbol": "BTC_USDT", "positionType": 2,
        "state": 3, "triggerSide": 2, "isFinished": 1,
        "stopLossType": 0, "stopLossPrice": "75375",
        "stopLossVol": 1, "profitLossVolType": "SEPARATE",
        "stopLossReverse": 2, "errorCode": 0,
        "updateTime": 1794050000200,
    }
    record.update(overrides)
    return record


def client():
    c = type("Client", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(),
    })()
    return c


def prepared_close():
    async def get_close(_client, _fingerprint, *, evidence_sink=None):
        evidence_sink.update(private())
        return good_close()
    return AsyncMock(side_effect=get_close)


async def get_result(c, rows, *, close=None):
    with (
        patch.object(close_audit, "audit_third_pilot",
                     new=close or prepared_close()),
        patch.object(native, "_check_journal", return_value={
            "stop_price": "75375",
        }),
        patch.object(pilot, "_history",
                     new=AsyncMock(return_value=(rows, 1))) as history,
    ):
        result = await audit.inspect_real_third_finished_stop(c, FP)
    if result.get("matching_finished_stop_row_count") is not None:
        history.assert_awaited_once()
    c._private_post.assert_not_awaited()
    return result


@pytest.mark.asyncio
async def test_exact_market_sl_terminal_generated_close_is_a_strict_witness():
    c = client()
    result = await get_result(c, [finished()])
    assert result["status"] == "THIRD_FINISHED_STOP_FORENSICS_OBSERVED_NONAUTHORIZING"
    assert result["account_bookended_flat_verified"] is True
    assert result["third_market_close_fills_verified"] is True
    assert result["finished_stop_state"] == "EXECUTED"
    assert result["finished_trigger_side"] == "STOP_LOSS"
    assert result["finished_stop_loss_type"] == "MARKET"
    assert result["finished_stop_price_matches_original_entry"] is True
    assert result["finished_stop_full_one_contract_volume"] is True
    assert result["generated_close_order_links_third_trade"] is True
    assert result["native_sl_trigger_origin_certified"] is True
    assert result["native_amendment_live_certified"] is False
    assert result["nonflat_restart_recovery_certified"] is False
    assert result["live_authorization"] is False
    assert "303" not in str(result)


@pytest.mark.asyncio
async def test_real_canceled_stop_with_nonzero_code_reports_exact_enum_and_sequence():
    result = await get_result(
        client(),
        [finished(
            state=2, triggerSide=0, placeOrderId=0, stopLossType=None,
            errorCode=5002, updateTime=1794050000400,
        )],
    )
    assert result["status"] == "THIRD_FINISHED_STOP_FORENSICS_OBSERVED_NONAUTHORIZING"
    assert result["finished_stop_state"] == "CANCELED"
    assert result["finished_stop_error_code"] == "NONZERO"
    assert result["finished_stop_error_code_value"] == 5002
    assert result["finished_stop_terminal_cancellation_observed"] is True
    assert result["finished_stop_update_vs_actual_close_fills"] == (
        "STOP_TERMINAL_UPDATE_AFTER_LAST_CLOSE_FILL"
    )
    assert result["stop_cancellation_caused_by_market_close_certified"] is False
    assert result["native_sl_trigger_origin_certified"] is False
    assert result["native_stop_market_type_certified"] is False
    assert result["mexc_orders_sent"] == 0


@pytest.mark.asyncio
async def test_stop_canceled_before_exit_cannot_be_described_as_after_close():
    result = await get_result(
        client(),
        [finished(
            state=2, triggerSide=0, placeOrderId=0,
            errorCode=3016, updateTime=1794049999000,
        )],
    )
    assert result["finished_stop_update_vs_actual_close_fills"] == (
        "STOP_TERMINAL_UPDATE_BEFORE_FIRST_CLOSE_FILL"
    )
    assert result["stop_cancellation_caused_by_market_close_certified"] is False
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_missing_stop_update_time_cannot_infer_cancellation_chronology():
    result = await get_result(
        client(),
        [finished(state=2, triggerSide=0, updateTime=None)],
    )
    assert result["finished_stop_update_vs_actual_close_fills"] == (
        "CHRONOLOGY_NOT_VERIFIABLE_FROM_VENUE_FIELDS"
    )
    assert result["stop_cancellation_caused_by_market_close_certified"] is False


@pytest.mark.asyncio
async def test_unknown_nonnegative_cancel_code_exposed_without_fake_interpretation():
    result = await get_result(
        client(), [finished(errorCode=73421, state=2, triggerSide=0)]
    )
    assert result["finished_stop_error_code_value"] == 73421
    assert result["finished_stop_error_code"] == "NONZERO"
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_market_type_missing_fails_without_defaulting_zero():
    result = await get_result(client(), [finished(stopLossType=None)])
    assert result["finished_stop_loss_type"] == "FIELD_NOT_REPORTED"
    assert result["native_stop_market_type_certified"] is False
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_tpsl_trigger_side_conflicts_with_sl_no_silent_override():
    result = await get_result(client(), [finished(triggerSide=1)])
    assert result["finished_trigger_side"] == "TAKE_PROFIT"
    assert result["native_sl_trigger_origin_certified"] is False
    assert result["native_stop_market_type_certified"] is True


@pytest.mark.parametrize("changes", [
    {"state": 2, "triggerSide": 0, "placeOrderId": 0},
    {"state": 4, "triggerSide": 0, "placeOrderId": 0},
    {"state": 5, "triggerSide": 0, "errorCode": 3016},
    {"stopLossType": 1},
    {"stopLossVol": "0.5"},
    {"stopLossPrice": "75200"},
    {"stopLossReverse": 1},
    {"placeOrderId": "399"},
    {"errorCode": 2000},
    {"isFinished": 0},
])
@pytest.mark.asyncio
async def test_canceled_invalidated_limit_partial_wrong_close_cannot_certify(changes):
    result = await get_result(client(), [finished(**changes)])
    assert result["native_sl_trigger_origin_certified"] is False
    assert result["live_authorization"] is False


@pytest.mark.asyncio
async def test_finished_stop_missing_bounded_history_reports_uncertainty():
    result = await get_result(client(), [])
    assert result["status"] == "FINISHED_STOP_NOT_FOUND_BOUNDED_HISTORY"
    assert result["finished_stop_history_complete"] is False
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_multiple_finished_tpsl_rows_cannot_infer_one_origin():
    result = await get_result(client(), [finished(), finished(id="405")])
    assert result["status"] == "MULTIPLE_FINISHED_STOP_ROWS_AMBIGUOUS"
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_unmatched_identity_fails_closed():
    result = await get_result(client(), [finished(positionId="999")])
    assert result["status"] == "FINISHED_STOP_IDENTITY_OR_SIDE_INVALID"
    assert result["native_sl_trigger_origin_certified"] is False


@pytest.mark.asyncio
async def test_exact_close_is_required_before_any_stop_history_get():
    c = client()
    bad = good_close()
    bad["status"] = "THIRD_CLOSE_FILL_NOT_INDEPENDENTLY_VERIFIED"
    with patch.object(
        close_audit, "audit_third_pilot",
        new=AsyncMock(return_value=bad),
    ), patch.object(pilot, "_history", new=AsyncMock()) as history:
        result = await audit.inspect_real_third_finished_stop(c, FP)
    assert result["status"] == "THIRD_CLOSE_NOT_VERIFIED_STOP_AUDIT_HALTED"
    history.assert_not_awaited()
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_prohibited_unjournaled_amendment_blocks_stop_witness():
    c = client()
    async def source(_c, _f, *, evidence_sink=None):
        p = private()
        p["journal_amend_post_attempted"] = True
        evidence_sink.update(p)
        return good_close()
    with patch.object(
        close_audit, "audit_third_pilot",
        new=AsyncMock(side_effect=source),
    ), patch.object(pilot, "_history", new=AsyncMock()) as history:
        result = await audit.inspect_real_third_finished_stop(c, FP)
    assert result["status"] == "PILOT_IDENTITY_OR_AMENDMENT_JOURNAL_CONFLICT"
    history.assert_not_awaited()
    c._private_post.assert_not_awaited()
