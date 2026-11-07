"""Zero-MEXC-POST offline guards for third one-shot native SL amendment test."""
from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest

from app import t100_web_pilot_native_stop_amendment_cli as m
from app import t100_web_pilot_tight_stop_trigger_cli as pilot

FP = "a" * 64
OID = "TPWEB" + "b" * 24


def old_journal():
    return {
        "version": 1,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": FP, "symbol": "BTC_USDT",
        "external_oid": OID, "contracts": 1, "open_type": 1,
        "stop_price": "75375.0", "stop_bps": "50",
        "reference_last_price": "75000",
        "entry_post_attempted": True,
        "entry_ack_verified": True, "entry_order_id": "12345",
        "close_post_attempted": False, "close_oid": None,
        "close_ack_verified": False, "status": "ENTRY_ACKED",
    }


def entry():
    return {
        "symbol": "BTC_USDT", "externalOid": OID,
        "orderId": "12345", "positionId": "98765",
        "side": 3, "state": 3, "openType": 1,
        "dealVol": 1, "vol": 1,
    }


def position():
    return {
        "symbol": "BTC_USDT", "positionId": "98765",
        "positionType": 2, "openType": 1,
        "holdVol": 1, "leverage": 1,
    }


def stop(price="75375.0"):
    return {
        "symbol": "BTC_USDT", "id": "56789",
        "orderId": "12345", "positionId": "98765",
        "positionType": 2, "state": 1,
        "isFinished": 0, "triggerSide": 0,
        "stopLossType": 0, "stopLossReverse": 2,
        "lossTrend": 1, "errorCode": 0,
        "profitLossVolType": "SEPARATE", "volType": 1,
        "stopLossVol": 1, "stopLossPrice": price,
    }


def counts(n=1):
    return {
        "sumCount": n, "stopOrderCount": n,
        "limitOrderCount": 0, "planOrderCount": 0,
        "trackOrderCount": 0,
    }


def quote():
    return {
        "symbol": "BTC_USDT", "lastPrice": "75000",
        "ask1": "75000.2", "timestamp": int(time.time() * 1000),
    }


def flat():
    return {
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": 0, "after_positions": 0,
        "before_current_tpsl": 0, "after_current_tpsl": 0,
        "reported_open_order_counts": counts(0),
    }


def fake_client():
    c = type("Client", (), {
        "_private_get": AsyncMock(),
        "_public_get": AsyncMock(),
        "_private_post": AsyncMock(return_value=None),
        "close": AsyncMock(),
    })()
    async def public_get(path, params=None):
        assert params == {"symbol": "BTC_USDT"}
        if path == "/api/v1/contract/ticker":
            return quote()
        if path == "/api/v1/contract/detail":
            return {"symbol": "BTC_USDT", "priceUnit": "0.1"}
        raise AssertionError("unexpected public endpoint")
    c._public_get.side_effect = public_get
    return c


@pytest.fixture
def journal_scope(tmp_path, monkeypatch):
    root = tmp_path / "T100WebPilot"
    root.mkdir()
    target = root / "native_stop_amendment_001.json"
    monkeypatch.setattr(pilot, "BASE_DIR", root)
    monkeypatch.setattr(pilot, "JOURNAL", target)
    monkeypatch.setattr(m, "THIRD_JOURNAL", target)
    monkeypatch.setattr(m, "ORIGINAL_JOURNAL", root / "tight_stop_trigger_001.json")
    pilot._write_first(old_journal())
    return target


def test_documented_endpoint_and_frozen_pilot_profile():
    assert m.AMEND_ENDPOINT == "/api/v1/private/stoporder/change_plan_price"
    assert m.INITIAL_STOP_BPS == Decimal(50)
    assert m.AMENDED_STOP_BPS == Decimal(30)
    assert m.AMENDED_STOP_BPS < m.INITIAL_STOP_BPS
    assert m.candidate_amended_stop(
        reference=Decimal("75000"),
        tick=Decimal("0.1"),
    ) == Decimal("75225.0")


def test_older_and_newer_journals_are_distinct(journal_scope):
    assert m.THIRD_JOURNAL.name == "native_stop_amendment_001.json"
    assert m.THIRD_JOURNAL.name not in (
        "tight_stop_trigger_001.json",
        "tight_stop_trigger_002.json",
    )
    assert journal_scope.exists()


def test_current_50bp_journal_readable_without_new_real_order(journal_scope):
    result = m._check_journal(FP)
    assert result["stop_bps"] == "50"
    assert not result.get("amend_post_attempted", False)


def test_journal_rejects_numeric_non_tightening_even_if_strings_sort(journal_scope):
    source = old_journal()
    source.update(
        amend_post_attempted=True, amend_ack_verified=False,
        amend_stop_order_id="56789", amend_previous_price="75375.0",
        amend_target_price="80000.0",
    )
    pilot._persist(source)
    with pytest.raises(pilot.TriggerPilotHalted, match="AMENDMENT_JOURNAL_PRICE_INVALID"):
        m._check_journal(FP)


def test_journal_rejects_ack_without_durable_post_attempt(journal_scope):
    source = old_journal()
    source["amend_ack_verified"] = True
    pilot._persist(source)
    with pytest.raises(pilot.TriggerPilotHalted, match="ACK_WITHOUT_POST_FENCE"):
        m._check_journal(FP)


def test_bad_credentials_stop_before_any_venue_get(journal_scope):
    with pytest.raises(pilot.TriggerPilotHalted, match="JOURNAL_ACCOUNT_OR_SHAPE"):
        m._check_journal("b" * 64)


@pytest.mark.asyncio
async def test_previous_second_pilot_full_market_exit_required(journal_scope):
    client = fake_client()
    result = {
        "status": "FULL_MARKET_CLOSE_CONFIRMED_BUT_TRIGGER_SIDE_TP_CONFLICT",
        "entry_filled": True, "account_stably_flat_verified": True,
        "generated_close_market_fill_verified": True,
        "position_count": 0, "stop_count": 0, "open_order_count": 0,
        "finished_trigger_side": "TP",
    }
    with (
        patch.object(pilot, "_status", new=AsyncMock(return_value=result)) as older,
        patch.object(m, "inventory_run", new=AsyncMock(return_value=flat())),
    ):
        outcome = await m.prior_pilot_market_close_and_flat(client, FP)
    assert outcome["previous_pilot_exact_market_close_verified"] is True
    assert outcome["prior_sl_trigger_side_conflict_retained"] is True
    assert older.await_count == 1
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_second_pilot_uncertain_close_blocks_new_entry(journal_scope):
    client = fake_client()
    result = {
        "status": "FLAT_NOT_VENUE_VERIFIED_SL_TRIGGER",
        "entry_filled": True, "account_stably_flat_verified": True,
        "generated_close_market_fill_verified": False,
        "position_count": 0, "stop_count": 0, "open_order_count": 0,
    }
    with patch.object(pilot, "_status", new=AsyncMock(return_value=result)):
        with pytest.raises(pilot.TriggerPilotHalted, match="MARKET_CLOSE_NOT_VERIFIED"):
            await m.prior_pilot_market_close_and_flat(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonflat_account_blocks_new_live_pilot(journal_scope):
    client = fake_client()
    result = {
        "status": "FULL_MARKET_CLOSE_CONFIRMED_BUT_TRIGGER_SIDE_TP_CONFLICT",
        "entry_filled": True, "account_stably_flat_verified": True,
        "generated_close_market_fill_verified": True,
        "position_count": 0, "stop_count": 0, "open_order_count": 0,
    }
    unstable = flat()
    unstable["after_positions"] = 1
    with (
        patch.object(pilot, "_status", new=AsyncMock(return_value=result)),
        patch.object(m, "inventory_run", new=AsyncMock(return_value=unstable)),
    ):
        with pytest.raises(pilot.TriggerPilotHalted, match="NOT_STABLY_FLAT"):
            await m.prior_pilot_market_close_and_flat(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_live_attached_market_stop_readback_exact_owned(journal_scope):
    client = fake_client()
    client._private_get.side_effect = [
        entry(), [position()], [stop()], counts(1),
    ]
    result = await m.inspect(client, FP)
    assert result["status"] == "ONE_ATTACHED_NATIVE_MARKET_STOP_READY_FOR_AMENDMENT"
    assert result["position_count"] == 1
    assert result["current_stop_count"] == 1
    assert result["original_stop_price_matches"] is True
    assert result["amendment_stop_reference_position_verified"] is True
    assert result["entry_allowed"] is False
    assert result["live_authorization"] is False
    client._private_post.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("stopLossType", 1),
    ("stopLossType", None),
    ("stopLossReverse", 1),
    ("stopLossVol", "0.5"),
    ("stopLossPrice", "75375.1"),
    ("profitLossVolType", "UNKNOWN"),
    ("positionId", "123"),
    ("orderId", "789"),
    ("errorCode", 8912),
])
@pytest.mark.asyncio
async def test_unprotected_or_changed_current_stop_blocks_amend(
    journal_scope,field,value,
):
    client = fake_client()
    wrong_stop = stop()
    wrong_stop[field] = value
    client._private_get.side_effect = [
        entry(), [position()], [wrong_stop], counts(1),
    ]
    result = await m.inspect(client, FP)
    assert result["status"] in (
        "ACTIVE_STOP_OR_POSITION_NOT_STRICT_FULL_MARKET",
        "ORIGINAL_NATIVE_STOP_CHANGED_OR_NOT_PROTECTIVE",
    )
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_real_amendment_post_fenced_before_send_and_get_confirmed(
    journal_scope,
):
    client = fake_client()
    state = {"price": "75375.0"}
    async def get(path, params=None):
        if path.startswith("/api/v1/private/order/external/"):
            return entry()
        if path == "/api/v1/private/position/open_positions":
            return [position()]
        if path == "/api/v1/private/stoporder/open_orders":
            return [stop(state["price"])]
        if path == "/api/v1/private/order/open_order_total_count":
            return counts(1)
        raise AssertionError("GET endpoint not allowlisted")
    client._private_get.side_effect = get
    async def post(path, payload, **kwargs):
        # One POST only AFTER append-only journal records attempt.
        assert path == m.AMEND_ENDPOINT
        assert kwargs.get("throttle") is False
        record = m._check_journal(FP)
        assert record["amend_post_attempted"] is True
        assert record["amend_ack_verified"] is False
        assert record["amend_target_price"] == "75225.0"
        assert payload == {
            "stopPlanOrderId": 56789,
            "stopLossPrice": "75225.0",
            "lossTrend": 1,
            "stopLossReverse": 2,
        }
        state["price"] = "75225.0"
        return None
    client._private_post.side_effect = post
    with patch.object(pilot, "_confirm") as confirm:
        result = await m.amend_once(client, FP)
    confirm.assert_called_once_with(
        "TIGHTEN MY ONE BTC STOP THROUGH MEXC API ONCE"
    )
    assert result["status"] == "NATIVE_STOP_AMENDMENT_READBACK_VERIFIED_RECONCILE_ONLY"
    assert result["amendment_same_stop_order_verified"]
    assert result["amendment_full_volume_market_protection_observed"]
    assert result["single_native_stop_edit_two_readbacks_verified"] is True
    assert result["exchange_continuous_protection_certified"] is False
    assert result["native_amendment_live_certified"] is False
    assert m._check_journal(FP)["amend_ack_verified"] is True
    assert client._private_post.await_count == 1
    with pytest.raises(pilot.TriggerPilotHalted, match="ALREADY_ATTEMPTED_NO_RETRY"):
        await m.amend_once(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_uncertain_real_amend_post_is_fenced_no_retry(journal_scope):
    client = fake_client()
    async def get(path, params=None):
        if path.startswith("/api/v1/private/order/external/"):
            return entry()
        if path == "/api/v1/private/position/open_positions":
            return [position()]
        if path == "/api/v1/private/stoporder/open_orders":
            return [stop()]
        if path == "/api/v1/private/order/open_order_total_count":
            return counts(1)
        raise AssertionError("unrecognized GET")
    client._private_get.side_effect = get
    client._private_post.side_effect = TimeoutError("unknown venue outcome")
    with patch.object(pilot, "_confirm"):
        with pytest.raises(TimeoutError):
            await m.amend_once(client, FP)
    assert m._check_journal(FP)["amend_post_attempted"] is True
    assert m._check_journal(FP)["amend_ack_verified"] is False
    with pytest.raises(pilot.TriggerPilotHalted, match="ALREADY_ATTEMPTED"):
        await m.amend_once(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_fails_closed_if_quote_past_amendment_target(journal_scope):
    client = fake_client()
    async def get(path, params=None):
        if path.startswith("/api/v1/private/order/external/"):
            return entry()
        if path == "/api/v1/private/position/open_positions":
            return [position()]
        if path == "/api/v1/private/stoporder/open_orders":
            return [stop()]
        if path == "/api/v1/private/order/open_order_total_count":
            return counts(1)
        raise AssertionError("unrecognized GET")
    async def public(path, params=None):
        if path == "/api/v1/contract/detail":
            return {"symbol": "BTC_USDT", "priceUnit": "0.1"}
        return {
            "symbol": "BTC_USDT", "lastPrice": "75200",
            "ask1": "75202",
            "timestamp": int(time.time()*1000),
        }
    client._private_get.side_effect = get
    client._public_get.side_effect = public
    with patch.object(pilot, "_confirm"):
        with pytest.raises(pilot.TriggerPilotHalted, match="NOT_SAFELY_ABOVE_MARKET"):
            await m.amend_once(client, FP)
    assert not m._check_journal(FP).get("amend_post_attempted",False)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_after_amendment_matching_price_and_same_stop(journal_scope):
    record = old_journal()
    record.update(amend_post_attempted=True, amend_ack_verified=True,
                  amend_stop_order_id="56789",
                  amend_previous_price="75375.0",
                  amend_target_price="75225.0")
    pilot._persist(record)
    client = fake_client()
    client._private_get.side_effect = [
        entry(), [position()], [stop("75225.0")], counts(1),
    ]
    output = await m.inspect(client, FP)
    assert output["status"] == "NATIVE_STOP_AMENDMENT_READBACK_VERIFIED_RECONCILE_ONLY"
    assert output["amendment_same_stop_order_verified"] is True
    assert output["amendment_target_price_matches"] is True
    assert output["amend_post_attempted"]
    assert output["amend_ack_verified"]
    assert not output["native_amendment_live_certified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_after_amendment_changed_stop_id_fails(journal_scope):
    record = old_journal()
    record.update(amend_post_attempted=True, amend_ack_verified=True,
                  amend_stop_order_id="56789",
                  amend_previous_price="75375.0",
                  amend_target_price="75225.0")
    pilot._persist(record)
    client = fake_client()
    replacement = stop("75225.0")
    replacement["id"] = "789"
    client._private_get.side_effect = [
        entry(), [position()], [replacement], counts(1),
    ]
    output = await m.inspect(client, FP)
    assert output["status"] == "NATIVE_STOP_AMENDMENT_UNCERTAIN_OR_INCOMPLETE"
    assert not output["amendment_same_stop_order_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_after_full_position_close_does_not_amend(journal_scope):
    client = fake_client()
    client._private_get.side_effect = [
        entry(), [], [], counts(0),
    ]
    result = await m.inspect(client, FP)
    assert result["status"] == "PILOT_POSITION_EXITED_RUN_ACCOUNT_FLAT_AUDIT"
    assert result["position_count"] == 0
    client._private_post.assert_not_awaited()
