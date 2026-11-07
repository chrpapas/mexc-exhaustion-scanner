"""Synthetic GET-only fifth pilot exit audit tests; no MEXC credentials."""
import asyncio

from app import t100_web_pilot_fifth_native_exit_audit_cli as audit


def _setup(monkeypatch, *, trigger_side=1, tp=None, manual_close=False,
           full_fill=True, flat=True, error_code=0):
    journal = {
        "version": 1,
        "external_oid": "TPWEB" + "a" * 24,
        "entry_order_id": "1001",
        "entry_post_attempted": True,
        "entry_ack_verified": True,
        "stop_price": "100.05",
        "stop_bps": "5",
        "close_post_attempted": manual_close,
    }
    entry = {
        "orderId": 1001,
        "positionId": 1002,
        "externalOid": journal["external_oid"],
        "symbol": "BTC_USDT",
        "side": 3,
        "state": 3,
        "openType": 1,
        "vol": 1,
        "dealVol": 1,
    }
    stop = {
        "id": 1005, "orderId": 1001, "positionId": 1002,
        "symbol": "BTC_USDT", "positionType": 2, "state": 3,
        "isFinished": 1, "stopLossType": 0,
        "profitLossVolType": "SEPARATE", "stopLossVol": 1,
        "stopLossPrice": "100.05", "takeProfitPrice": tp,
        "triggerSide": trigger_side, "errorCode": error_code,
        "placeOrderId": 1003,
    }
    async def inventory(*, client):
        return "flat" if flat else "not-flat"
    async def history(_safe, eid, pid):
        assert eid == "1001" and pid == "1002"
        return [stop], 1
    async def verify(_safe, *, close_order_id, entry_position_id, journal):
        assert close_order_id == "1003"
        assert entry_position_id == "1002"
        return {
            "exact_close_order_filled": full_fill,
            "close_market_order_type_verified": full_fill,
            "closed_contract_quantity_exact_one": full_fill,
            "close_api_error": "ZERO",
            "exact_close_trade_deals_filled": full_fill,
            "close_deal_count": 1 if full_fill else 0,
        }
    class Safe:
        def __init__(self, client):
            pass
        async def get(self, path):
            if path.endswith(journal["external_oid"]):
                return entry
            raise AssertionError("Unexpected GET route: " + path)
    class Client:
        async def _private_post(self, *args, **kwargs):
            raise AssertionError("No POST allowed")

    monkeypatch.setattr(audit, "_read_fifth_journal", lambda _fp: journal)
    monkeypatch.setattr(audit, "run_inventory", inventory)
    monkeypatch.setattr(audit, "_is_flat", lambda state: state == "flat")
    monkeypatch.setattr(audit, "HistoryGetOnly", Safe)
    monkeypatch.setattr(audit.pilot, "_history", history)
    monkeypatch.setattr(audit, "_verify_exact_close", verify)
    return Client()


def test_exact_attached_exit_with_tp_field_conflict(monkeypatch):
    client = _setup(monkeypatch)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["status"] == (
        "FIFTH_NATIVE_ATTACHED_EXIT_EXACT_MARKET_FILL_VERIFIED"
        "_TP_CLASSIFICATION_CONFLICT_NONAUTHORIZING"
    )
    assert report["native_attached_exit_execution_correlated"] is True
    assert report["historical_trigger_side"] == "TP"
    assert report["no_take_profit_price_observed"] is True
    assert report["native_sl_trigger_certified"] is False
    assert report["live_authorization"] is False
    assert report["mexc_orders_sent"] == 0


def test_sl_classification_with_full_fill_only_offline_certifies_evidence(monkeypatch):
    client = _setup(monkeypatch, trigger_side=2)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["native_sl_trigger_certified"] is True
    assert report["live_authorization"] is False
    assert report["entry_allowed"] is False


def test_missing_generated_fill_blocks_certification(monkeypatch):
    client = _setup(monkeypatch, full_fill=False)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["status"] == "FIFTH_GENERATED_EXIT_OR_FLATNESS_UNVERIFIED"
    assert report["native_attached_exit_execution_correlated"] is False
    assert report["native_sl_trigger_certified"] is False


def test_manual_close_disallows_native_event_attribution(monkeypatch):
    client = _setup(monkeypatch, manual_close=True)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["native_attached_exit_execution_correlated"] is False
    assert report["native_sl_trigger_certified"] is False


def test_nonflat_account_halts(monkeypatch):
    client = _setup(monkeypatch, flat=False)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["status"] == "FIFTH_ACCOUNT_NOT_FLAT_AUDIT_HALTED"
    assert report["live_authorization"] is False


def test_nonzero_stop_error_blocks_strict_sl_certification(monkeypatch):
    client = _setup(monkeypatch, trigger_side=2, error_code=8912)
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["native_attached_exit_execution_correlated"] is True
    assert report["native_sl_trigger_certified"] is False
    assert report["live_authorization"] is False


def test_real_tp_price_cannot_be_called_sl_classification_conflict(monkeypatch):
    client = _setup(monkeypatch, trigger_side=1, tp="99.95")
    report = asyncio.run(audit.audit_fifth(client, "synthetic"))
    assert report["status"] == (
        "FIFTH_NATIVE_ATTACHED_EXIT_TP_LEG_POSSIBLE_NONAUTHORIZING"
    )
    assert report["native_sl_trigger_certified"] is False
