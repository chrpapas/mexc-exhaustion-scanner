"""Offline-only one-shot fourth REAL pilot safety; never send a real MEXC POST."""
from __future__ import annotations

import time
from decimal import Decimal as D
from unittest.mock import AsyncMock, patch

import pytest

from app import t100_web_pilot_fourth_supervised_amendment_cli as pilot4
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_third_close_audit_cli as prior

FP = "f" * 64
OID = "TPWEB" + "b" * 24


def journal():
    return {
        "version": 1, "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": FP, "symbol": "BTC_USDT",
        "external_oid": OID, "contracts": 1, "open_type": 1,
        "stop_price": "75375.0", "stop_bps": "50",
        "reference_last_price": "75000",
        "entry_post_attempted": True, "entry_ack_verified": True,
        "entry_order_id": "12345", "close_post_attempted": False,
        "close_oid": None, "close_ack_verified": False,
        "status": "ENTRY_ACKED",
    }


def entry():
    return {
        "symbol": "BTC_USDT", "externalOid": OID, "orderId": "12345",
        "positionId": "98765", "side": 3, "state": 3,
        "openType": 1, "dealVol": 1, "vol": 1,
    }


def position():
    return {
        "symbol": "BTC_USDT", "positionId": "98765",
        "positionType": 2, "openType": 1,
        "holdVol": 1, "leverage": 1,
    }


def stop(*, price="75375.0", stop_type=None, stop_id="56789"):
    item = {
        "symbol": "BTC_USDT", "id": stop_id,
        "orderId": "12345", "positionId": "98765",
        "positionType": 2, "state": 1,
        "isFinished": 0, "triggerSide": 0,
        "stopLossReverse": 2, "lossTrend": 1,
        "errorCode": 0, "volType": 1,
        "stopLossOrderPrice": "0",
        "profitLossVolType": "SEPARATE",
        "stopLossVol": 1, "stopLossPrice": price,
    }
    if stop_type is not None:
        item["stopLossType"] = stop_type
    return item


def counts(n=1):
    return {
        "sumCount": n, "stopOrderCount": n,
        "limitOrderCount": 0, "planOrderCount": 0,
        "trackOrderCount": 0,
    }


def quote():
    return {
        "symbol": "BTC_USDT",
        "lastPrice": "75000", "ask1": "75000.1",
        "timestamp": int(time.time() * 1000),
    }


def fake_client(*, st=None, secondary=None, second_type=None):
    c = type("Client", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(return_value={"success": True}),
        "_public_get": AsyncMock(),
        "close": AsyncMock(),
    })()
    current = st or stop()
    other = secondary or stop(stop_type=second_type)
    async def getter(path, params=None):
        if path.startswith("/api/v1/private/order/external/"):
            return entry()
        if path == "/api/v1/private/position/open_positions":
            return [position()]
        if path == "/api/v1/private/stoporder/open_orders":
            return [current]
        if path == "/api/v1/private/order/open_order_total_count":
            return counts()
        if path == "/api/v1/private/stoporder/list/orders":
            assert params == {
                "symbol": "BTC_USDT", "is_finished": 0,
                "page_num": 1, "page_size": 100,
            }
            return [other]
        raise AssertionError("unexpected GET " + path)
    async def public(path, params=None):
        assert params == {"symbol": "BTC_USDT"}
        if path == "/api/v1/contract/ticker":
            return quote()
        if path == "/api/v1/contract/detail":
            return {"symbol": "BTC_USDT", "priceUnit": "0.1"}
        raise AssertionError("unexpected public request")
    c._private_get.side_effect = getter
    c._public_get.side_effect = public
    return c


@pytest.fixture
def scope(tmp_path, monkeypatch):
    root = tmp_path / "T100-web-test"
    root.mkdir(mode=0o700)
    old = pilot.JOURNAL
    target = root / "supervised_native_stop_amendment_004.json"
    monkeypatch.setattr(pilot, "BASE_DIR", root)
    monkeypatch.setattr(pilot, "JOURNAL", target)
    monkeypatch.setattr(pilot4, "FOURTH_JOURNAL", target)
    pilot._write_first(journal())
    return target


def test_fourth_exact_journal_not_third_or_second(scope):
    result = pilot4._read_journal(FP)
    assert result["stop_bps"] == "50"
    assert scope.name == "supervised_native_stop_amendment_004.json"
    assert scope.name != "native_stop_amendment_001.json"


@pytest.mark.asyncio
async def test_unknown_market_type_allowed_only_for_supervised_experiment(scope):
    c = fake_client()
    result, sid, price = await pilot4._observe_stop(c, FP)
    assert result["status"] == pilot4.STATUS_READY
    assert sid == "56789" and price == D("75375.0")
    assert result["market_type_missing_accepted_only_for_supervised_experiment"] is True
    assert result["operator_ui_market_stop_confirmation_required"] is True
    assert result["second_official_get_exact_identity"] is True
    assert result["second_official_get_stop_type"] == "FIELD_NOT_REPORTED"
    assert result["native_stop_market_type_certified"] is False
    assert result["native_amendment_live_certified"] is False
    assert result["live_authorization"] is False
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_market_type_accepted_but_still_nonproduction(scope):
    c = fake_client(st=stop(stop_type=0), second_type=0)
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == pilot4.STATUS_READY
    assert sid == "56789"
    assert result["market_type_explicit_from_api"] is True
    assert result["live_authorization"] is False
    c._private_post.assert_not_awaited()


@pytest.mark.parametrize("wrong", [1, True, False, "0", -1, 2])
@pytest.mark.asyncio
async def test_limit_or_malformed_stop_type_not_accepted_even_for_experiment(
    scope, wrong,
):
    c = fake_client(st=stop(stop_type=wrong))
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_STOP_NOT_MARKET_OR_UNKNOWN_TYPE_UNSAFE"
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_route_reporting_limit_blocks_experiment(scope):
    c = fake_client(secondary=stop(stop_type=1))
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_SECOND_SIGNED_STOP_GET_INCONCLUSIVE"
    assert result["second_official_get_stop_type"] == "LIMIT"
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_extra_sl_limit_price_rejects_even_with_missing_type(scope):
    c = fake_client(st={**stop(), "stopLossOrderPrice": "75390.0"})
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_EXACT_NATIVE_STOP_INVARIANT_FAILED"
    assert result["failed_strict_fields"] == ["limit_sl_order_price_not_positive"]
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_volume_both_endpoints_cannot_be_promoted(scope):
    c = fake_client(st={**stop(), "stopLossVol": 2})
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_EXACT_NATIVE_STOP_INVARIANT_FAILED"
    assert "stop_loss_quantity_full_one" in result["failed_strict_fields"]
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatch_stop_id_in_second_route_blocks(scope):
    c = fake_client(secondary=stop(stop_id="99999"))
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_SECOND_SIGNED_STOP_GET_INCONCLUSIVE"
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_below_ask_does_not_appear_protective(scope):
    c = fake_client(st=stop(price="74999.9"), secondary=stop(price="74999.9"))
    result, sid, _ = await pilot4._observe_stop(c, FP)
    assert result["status"] == "FOURTH_STOP_PRICE_INVALID_OR_ALREADY_TRIGGERED"
    assert sid is None
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_declines_manual_ui_attestation_prevents_any_post(
    scope,
):
    c = fake_client()
    with patch.object(pilot, "_confirm",
                      side_effect=pilot.TriggerPilotHalted(
                          "OPERATOR_CONFIRMATION_DECLINED"
                      )):
        with pytest.raises(
            pilot.TriggerPilotHalted, match="OPERATOR_CONFIRMATION_DECLINED"
        ):
            await pilot4.amend_once(c, FP)
    c._private_post.assert_not_awaited()
    assert pilot4._read_journal(FP).get("amend_post_attempted") is not True


@pytest.mark.asyncio
async def test_real_amend_post_fenced_once_before_exchange_send(scope):
    c = fake_client()
    old_get = c._private_get.side_effect
    posted: list[dict] = []
    async def post(path, args, **kwargs):
        posted.append(dict(args))
        body = pilot4._read_journal(FP)
        assert body["amend_post_attempted"] is True
        assert body["amend_ack_verified"] is False
        assert body["amend_stop_order_id"] == "56789"
        assert body["amend_target_price"] == "75225.0"
        assert path == native.AMEND_ENDPOINT
        return {"success": True}
    c._private_post.side_effect = post

    async def mutate_get(path, params=None):
        # Current exchange stop price only changes AFTER actual POST ACK.
        if path == "/api/v1/private/stoporder/open_orders" and posted:
            return [stop(price="75225.0")]
        if path == "/api/v1/private/stoporder/list/orders" and posted:
            return [stop(price="75225.0")]
        return await old_get(path, params)
    c._private_get.side_effect = mutate_get
    with patch.object(pilot, "_confirm") as confirmation:
        result = await pilot4.amend_once(c, FP)
    assert confirmation.call_args.args == (pilot4.CONFIRM_PHRASE,)
    assert len(posted) == 1
    assert posted[0] == {
        "stopPlanOrderId": 56789, "stopLossPrice": "75225.0",
        "lossTrend": 1, "stopLossReverse": 2,
    }
    assert result["status"] == pilot4.STATUS_EDITED
    assert result["one_exact_stop_id_two_independent_post_edit_readbacks"] is True
    assert result["native_stop_market_type_certified"] is False
    assert result["native_amendment_live_certified"] is False
    assert result["exchange_continuous_protection_certified"] is False
    assert pilot4._read_journal(FP)["amend_ack_verified"] is True


@pytest.mark.asyncio
async def test_uncertain_exchange_post_never_retries_or_resets_fence(scope):
    c = fake_client()
    c._private_post.side_effect = TimeoutError("unknown")
    with patch.object(pilot, "_confirm"):
        with pytest.raises(TimeoutError, match="unknown"):
            await pilot4.amend_once(c, FP)
    record = pilot4._read_journal(FP)
    assert record["amend_post_attempted"] is True
    assert record["amend_ack_verified"] is False
    assert c._private_post.await_count == 1
    with pytest.raises(
        pilot.TriggerPilotHalted, match="ALREADY_ATTEMPTED_NO_RETRY"
    ):
        await pilot4.amend_once(c, FP)
    assert c._private_post.await_count == 1


@pytest.mark.asyncio
async def test_changed_original_stop_price_after_two_valid_gets_no_mutation(scope):
    c = fake_client(st=stop(price="75400.0"), secondary=stop(price="75400.0"))
    with patch.object(pilot, "_confirm"):
        with pytest.raises(
            pilot.TriggerPilotHalted, match="PREFLIGHT_FAILED"
        ):
            await pilot4.amend_once(c, FP)
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_prior_third_close_verified_before_fresh_order_check(scope):
    c = fake_client()
    prior_status = {
        "status": "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING",
        "account_bookended_flat_verified": True,
        "exact_close_order_filled": True,
        "exact_close_trade_deals_filled": True,
    }
    inventory = {
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": 0, "after_positions": 0,
        "before_current_tpsl": 0, "after_current_tpsl": 0,
        "reported_open_order_counts": counts(0),
    }
    with (
        patch.object(prior, "audit_third_pilot",
                     new=AsyncMock(return_value=prior_status)) as previously,
        patch.object(pilot4, "inventory_run",
                     new=AsyncMock(return_value=inventory)),
    ):
        result = await pilot4._prior_close_and_flat(c, FP)
    assert result["third_pilot_real_close_confirmed"] is True
    assert result["current_web_subaccount_stably_flat"] is True
    assert result["fourth_journal_exists"] is True
    assert previously.await_count == 1
    c._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_existing_journal_blocks_new_entry_before_real_order(
    scope,
):
    c = fake_client()
    with (
        patch.object(pilot4, "_prior_close_and_flat",
                     new=AsyncMock(return_value={
                         "fourth_journal_exists": True,
                     })),
        patch.object(pilot, "_send", new=AsyncMock()) as send,
        patch.object(pilot4, "_allow_real_mode_env"),
        patch.object(pilot4, "read_pilot_keychain_credentials",
                     return_value=("scoped-fake-key", "not-real")),
        patch.object(pilot4, "MexcTradeClient", return_value=c),
        patch.object(pilot4.sys, "platform", "darwin"),
    ):
        with pytest.raises(pilot.TriggerPilotHalted, match="EXISTS_NO_ENTRY_RETRY"):
            await pilot4._main("open")
    send.assert_not_awaited()
    c._private_post.assert_not_awaited()
