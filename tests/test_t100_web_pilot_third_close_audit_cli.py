"""Offline-only validation for real third-pilot filled close and flat forensic."""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch
import pytest

from app import t100_web_pilot_third_close_audit_cli as audit
from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_native_stop_amendment_cli as native

FP = "a" * 64
OID = "TPWEB" + "b" * 24
CLOSE_OID = "TPWEB" + "c" * 24
ENTRY_ID = "11111"
POSITION_ID = "87654"
CLOSE_ID = "22222"


def journal():
    return {
        "version": 1, "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": FP, "symbol": "BTC_USDT",
        "external_oid": OID, "contracts": 1, "open_type": 1,
        "stop_price": "75375.0", "stop_bps": "50",
        "reference_last_price": "75000.0",
        "entry_post_attempted": True, "entry_ack_verified": True,
        "entry_order_id": ENTRY_ID,
        "close_post_attempted": False, "close_oid": None,
        "close_ack_verified": False,
    }


def entry():
    return {
        "externalOid": OID, "orderId": ENTRY_ID,
        "positionId": POSITION_ID, "symbol": "BTC_USDT",
        "side": 3, "openType": 1, "state": 3,
        "vol": 1, "dealVol": 1,
    }


def closed_order():
    return {
        "externalOid": "MANUAL_OR_VENUE_EXTERNAL",
        "orderId": CLOSE_ID, "positionId": POSITION_ID,
        "symbol": "BTC_USDT", "side": 2,
        "state": 3, "openType": 1,
        "orderType": 5, "vol": 1,
        "dealVol": 1, "errorCode": 0,
    }


def deals():
    return [{
        "id": "45677", "orderId": CLOSE_ID,
        "symbol": "BTC_USDT", "side": 2,
        "vol": 1, "price": "75221.1",
        "timestamp": 1794050000000,
        "taker": True,
    }]


def flat():
    return {
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": 0, "after_positions": 0,
        "before_current_tpsl": 0, "after_current_tpsl": 0,
        "reported_open_order_counts": {
            "sumCount": 0, "limitOrderCount": 0,
            "stopOrderCount": 0, "planOrderCount": 0,
            "trackOrderCount": 0,
        },
    }


def fake_client(history=None, order=None, fills=None):
    c = type("Client", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(),
    })()
    async def private_get(path, params=None):
        if path.startswith("/api/v1/private/order/external/"):
            assert params is None
            return entry()
        if path == audit.HISTORY_PATH:
            assert params == {
                "symbol": "BTC_USDT", "states": "3",
                "page_num": 1, "page_size": 100,
            }
            return history if history is not None else [
                {"orderId": ENTRY_ID, "positionId": POSITION_ID,
                 "symbol": "BTC_USDT", "side": 3, "state": 3},
                {"orderId": CLOSE_ID, "positionId": POSITION_ID,
                 "symbol": "BTC_USDT", "side": 2, "state": 3},
            ]
        if path == f"/api/v1/private/order/get/{CLOSE_ID}":
            assert params is None
            return order if order is not None else closed_order()
        if path == f"/api/v1/private/order/deal_details/{CLOSE_ID}":
            assert params is None
            return fills if fills is not None else deals()
        raise AssertionError("unexpected signed GET route " + path)
    c._private_get.side_effect = private_get
    return c


@pytest.mark.asyncio
async def test_real_one_contract_full_exit_verified_from_signed_order_and_fills():
    client = fake_client()
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())) as inventory,
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == (
        "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
    )
    assert result["third_pilot_entry_filled_verified"] is True
    assert result["filled_close_candidate_count"] == 1
    assert result["exact_close_order_filled"] is True
    assert result["exact_close_trade_deals_filled"] is True
    assert result["close_market_order_type_verified"] is True
    assert result["taker_close_trade_observed"] is True
    assert result["close_deal_count"] == 1
    assert result["account_bookended_flat_verified"] is True
    assert result["all_historical_orders_complete"] is False
    assert result["close_origin"] == "UNKNOWN_OR_EXTERNAL"
    assert result["native_sl_trigger_origin_certified"] is False
    assert result["live_authorization"] is False
    assert result["mexc_orders_sent"] == 0
    assert result["read_only"] is True
    assert inventory.await_count == 2
    assert CLOSE_ID not in str(result)
    assert POSITION_ID not in str(result)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_signed_close_evidence_sink_binds_exact_ids_and_fills_without_report_leak():
    client = fake_client()
    evidence: dict = {}
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory", new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(
            client, FP, evidence_sink=evidence,
        )
    assert result["status"] == (
        "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
    )
    assert evidence["close_order_id"] == CLOSE_ID
    assert evidence["position_id"] == POSITION_ID
    assert evidence["entry_order_id"] == ENTRY_ID
    assert evidence["entry_external_oid"] == OID
    assert evidence["account_fingerprint_sha256"] == FP
    assert evidence["close_deals"][0]["deal_id"] == "45677"
    assert evidence["close_deals"][0]["fill_price"] == "75221.1"
    assert evidence["close_deals"][0]["qty"] == "1"
    assert evidence["journal_amend_post_attempted"] is False
    assert CLOSE_ID not in str(result)
    assert POSITION_ID not in str(result)
    assert "75221.1" not in str(result)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_venue_fill_cannot_leave_recovery_evidence_sink():
    client = fake_client(fills=[{
        "id": "45677", "orderId": "888888",
        "symbol": "BTC_USDT", "side": 2, "vol": 1,
        "price": "75221.1", "timestamp": 1794050000000, "taker": True,
    }])
    evidence: dict = {}
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory", new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(
            client, FP, evidence_sink=evidence,
        )
    assert result["status"] == "THIRD_CLOSE_FILL_NOT_INDEPENDENTLY_VERIFIED"
    assert evidence == {}
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_second_bookend_drift_clears_private_recovery_evidence():
    client = fake_client()
    unsafe = flat()
    unsafe["after_current_tpsl"] = 1
    evidence: dict = {}
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(side_effect=[flat(), unsafe])),
    ):
        result = await audit.audit_third_pilot(
            client, FP, evidence_sink=evidence,
        )
    assert result["status"] == "ACCOUNT_FLAT_DRIFTED_DURING_AUDIT"
    assert evidence == {}
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_recorded_close_owned_external_oid_identified():
    client = fake_client()
    item = closed_order()
    item["externalOid"] = CLOSE_OID
    record = journal()
    record["close_post_attempted"] = True
    record["close_oid"] = CLOSE_OID
    record["close_ack_verified"] = True
    with (
        patch.object(native, "_check_journal", return_value=record),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        # Supply exact close order response to existing signed route
        orig = client._private_get.side_effect
        async def override(path, params=None):
            if path == f"/api/v1/private/order/get/{CLOSE_ID}":
                return item
            return await orig(path, params)
        client._private_get.side_effect = override
        result = await audit.audit_third_pilot(client, FP)
    assert result["close_origin"] == "MATCHES_THIRD_PILOT_SCRIPT_CLOSE"
    assert result["exact_close_trade_deals_filled"] is True
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonflat_account_stops_audit_without_assuming_any_close():
    client = fake_client()
    bad = flat()
    bad["after_positions"] = 1
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=bad)),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "ACCOUNT_NOT_FLAT_CLOSE_AUDIT_HALTED"
    assert result["third_pilot_entry_filled_verified"] is False
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_user_or_entry_id_never_adopts_other_position():
    client = fake_client()
    wrong = entry()
    wrong["externalOid"] = CLOSE_OID
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        orig = client._private_get.side_effect
        async def bad(path, params=None):
            if path.startswith("/api/v1/private/order/external/"):
                return wrong
            return await orig(path, params)
        client._private_get.side_effect = bad
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "THIRD_ENTRY_OR_ORIGIN_NOT_EXACT"
    assert not result["third_pilot_entry_filled_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_position_in_historical_close_not_considered_our_exit():
    client = fake_client(history=[
        {"orderId": "33333", "positionId": "111111",
         "symbol": "BTC_USDT", "side": 2, "state": 3},
    ])
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "EXACT_CLOSE_NOT_FOUND_IN_BOUNDED_HISTORY"
    assert result["filled_close_candidate_count"] == 0
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_full_close_candidates_dont_claim_unique_one_contract_exit():
    client = fake_client(history=[
        {"orderId": CLOSE_ID, "positionId": POSITION_ID,
         "symbol": "BTC_USDT", "side": 2, "state": 3},
        {"orderId": "33333", "positionId": POSITION_ID,
         "symbol": "BTC_USDT", "side": 2, "state": 3},
    ])
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "MULTIPLE_FILLED_CLOSE_ORDERS_RECONCILE_MANUALLY"
    assert result["filled_close_candidate_count"] == 2
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_array_is_not_certified_exhaustive_history():
    client = fake_client()
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["historical_pages_read"] == 1
    assert result["all_historical_orders_complete"] is False


def test_paged_api_wrapper_requires_exact_page_contract():
    rows = [{
        "orderId": CLOSE_ID, "positionId": POSITION_ID,
        "symbol": "BTC_USDT", "side": 2, "state": 3,
    }]
    parsed, complete = audit._parse_page({
        "resultList": rows, "currentPage": 1,
        "totalPage": 1, "pageSize": 100,
        "totalCount": 1,
    }, 1)
    assert complete is True
    assert parsed == rows
    with pytest.raises(pilot.TriggerPilotHalted, match="METADATA_UNVERIFIED"):
        audit._parse_page({
            "resultList": rows, "currentPage": 1,
            "totalPage": 0, "pageSize": 100, "totalCount": 1,
        }, 1)


@pytest.mark.asyncio
async def test_full_close_order_but_wrong_deal_id_or_volume_not_certified():
    client = fake_client(fills=[{
        "id": "45677", "orderId": "888888",
        "symbol": "BTC_USDT", "side": 2, "vol": 1,
        "price": "75221.1", "timestamp": 1794050000000,
        "taker": True,
    }])
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(return_value=flat())),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "THIRD_CLOSE_FILL_NOT_INDEPENDENTLY_VERIFIED"
    assert result["exact_close_order_filled"] is True
    assert result["exact_close_trade_deals_filled"] is False
    assert result["close_market_order_type_verified"] is True
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_account_bookend_drift_prevents_exit_success():
    client = fake_client()
    changed = flat()
    changed["after_positions"] = 1
    with (
        patch.object(native, "_check_journal", return_value=journal()),
        patch.object(audit, "run_inventory",
                     new=AsyncMock(side_effect=[flat(), changed])),
    ):
        result = await audit.audit_third_pilot(client, FP)
    assert result["status"] == "ACCOUNT_FLAT_DRIFTED_DURING_AUDIT"
    assert result["account_bookended_flat_verified"] is False
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_get_allowlist_never_exposes_mutation():
    client = fake_client()
    safe = audit.HistoryGetOnly(client)
    assert not hasattr(safe, "_private_post")
    with pytest.raises(pilot.TriggerPilotHalted):
        await safe.get("/api/v1/private/order/create")
    with pytest.raises(pilot.TriggerPilotHalted):
        await safe.get(audit.HISTORY_PATH, {
            "symbol": "BTC_USDT", "states": "1",
            "page_num": 1, "page_size": 100,
        })
    with pytest.raises(pilot.TriggerPilotHalted):
        await safe.get("/api/v1/private/order/get/0")
    client._private_post.assert_not_awaited()
