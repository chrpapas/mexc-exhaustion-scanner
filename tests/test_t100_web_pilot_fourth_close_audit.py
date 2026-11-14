"""Offline-only fourth-pilot GET forensic regression tests.

Synthetic venue responses; no credentials, network calls or order mutation.
"""
import asyncio

from app import t100_web_pilot_fourth_close_audit_cli as audit


def _scenario(monkeypatch, *, stop_price="103", close_external_exact=True):
    journal = {
        "external_oid": "TPWEB" + "a" * 24,
        "close_oid": "TPWEB" + "b" * 24,
        "amend_post_attempted": True,
        "amend_ack_verified": True,
        "ui_operator_confirmed_market_sl_before_amend": True,
        "amend_target_price": "103",
        "amend_stop_order_id": "123",
        "stop_price": "105",
        "close_post_attempted": True,
        "entry_order_id": "1001",
    }
    entry = {
        "orderId": 1001, "positionId": 1002, "externalOid": journal["external_oid"],
        "symbol": "BTC_USDT", "side": 3, "state": 3,
        "openType": 1, "vol": 1, "dealVol": 1,
    }
    close = {
        "orderId": 1003, "positionId": 1002,
        "externalOid": (
            journal["close_oid"] if close_external_exact else "TPWEB" + "c" * 24
        ),
        "symbol": "BTC_USDT", "side": 2,
    }
    finished = {
        "id": 123, "orderId": 1001, "positionId": 1002,
        "symbol": "BTC_USDT", "positionType": 2,
        "profitLossVolType": "SEPARATE", "stopLossVol": 1,
        "stopLossPrice": stop_price, "state": 2,
        "triggerSide": 0, "isFinished": 1,
        "errorCode": 2031,
    }

    async def inventory(*, client):
        return "flat"

    async def history(_safe, _eid, _pid):
        return [finished], 1

    async def close_check(_safe, *, close_order_id, entry_position_id, journal):
        assert close_order_id == "1003"
        assert entry_position_id == "1002"
        return {
            "exact_close_order_filled": True,
            "exact_close_trade_deals_filled": True,
            "close_market_order_type_verified": True,
            "closed_contract_quantity_exact_one": True,
            "close_api_error": "ZERO", "close_deal_count": 1,
        }

    class Safe:
        def __init__(self, client):
            pass

        async def get(self, path):
            if journal["external_oid"] in path:
                return entry
            if journal["close_oid"] in path:
                return close
            raise AssertionError("Unexpected GET route: " + path)

    class Client:
        async def _private_post(self, *args, **kwargs):
            raise AssertionError("No MEXC mutation is permitted")

    monkeypatch.setattr(audit, "_fourth_journal", lambda _fp: journal)
    monkeypatch.setattr(audit, "run_inventory", inventory)
    monkeypatch.setattr(audit, "_is_flat", lambda x: x == "flat")
    monkeypatch.setattr(audit, "HistoryGetOnly", Safe)
    monkeypatch.setattr(audit, "_verify_exact_close", close_check)
    monkeypatch.setattr(audit.pilot, "_history", history)
    return Client()


def test_exact_manual_close_and_canceled_amended_stop(monkeypatch):
    client = _scenario(monkeypatch)
    result = asyncio.run(audit.audit_fourth_pilot(client, "synthetic"))
    assert result["status"] == (
        "FOURTH_EXACT_MANUAL_MARKET_CLOSE_AND_EDITED_STOP_CANCELED_VERIFIED"
        "_NONAUTHORIZING"
    )
    assert result["account_bookended_flat_verified"] is True
    assert result["finished_stop_price_matches_amended_target"] is True
    assert result["finished_stop_error_code"] == "NONZERO_REPORTED"
    assert result["manual_market_close_exactly_verified"] is True
    assert result["operator_ui_trigger_market_sl_confirmed_before_amend"] is True
    assert result["supervised_native_stop_full_one_contract_volume_observed"] is True
    assert result["native_sl_trigger_and_market_close_verified"] is False
    assert result["live_authorization"] is False


def test_original_stop_price_must_not_replace_amended_price(monkeypatch):
    client = _scenario(monkeypatch, stop_price="105")
    result = asyncio.run(audit.audit_fourth_pilot(client, "synthetic"))
    assert result["manual_market_close_exactly_verified"] is True
    assert result["finished_stop_price_matches_amended_target"] is False
    assert result["status"] == (
        "MANUAL_MARKET_CLOSE_VERIFIED_STOP_HISTORY_INCONCLUSIVE"
    )
    assert result["live_authorization"] is False


def test_unrelated_close_external_id_is_rejected(monkeypatch):
    client = _scenario(monkeypatch, close_external_exact=False)
    result = asyncio.run(audit.audit_fourth_pilot(client, "synthetic"))
    assert result["status"] == "FOURTH_JOURNALED_CLOSE_ORDER_NOT_EXACT"
    assert result["live_authorization"] is False


def test_ui_confirmation_and_full_volume_are_distinct_from_api_market_type(monkeypatch):
    client = _scenario(monkeypatch)
    result = asyncio.run(audit.audit_fourth_pilot(client, "synthetic"))
    assert result["operator_ui_trigger_market_sl_confirmed_before_amend"]
    assert result["supervised_native_stop_full_one_contract_volume_observed"]
    assert not result["exchange_continuous_protection_certified"]
    assert not result["native_amendment_live_certified_for_production"]
    assert not result["live_authorization"]
