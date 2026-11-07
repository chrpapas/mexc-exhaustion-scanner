"""All-fake safety tests for the dedicated live 5bps stop-trigger pilot."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import t100_web_pilot_tight_stop_trigger_cli as m

OID = "TPWEB" + "a" * 24
CLOSE_OID = "TPWEB" + "b" * 24
FP = "d" * 64


def preview():
    return {
        "status": "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED",
        "reference_last_price": "75920.8",
        "stop_loss_price": "75958.8",
        "tick": "0.1",
        "stop_bps": "5",
        "notional_usdt": "7.5921",
        "contracts": 1,
        "margin": "ISOLATED",
        "leverage": "1x",
        "max_notional_usdt": "12.50",
        "max_share_available": "25%",
        "mexc_orders_sent": 0,
        "live_authorization": False,
    }


def entry():
    return {
        "externalOid": OID, "orderId": "123456",
        "positionId": "87654", "symbol": "BTC_USDT",
        "side": 3, "state": 3, "openType": 1,
        "vol": 1, "dealVol": 1,
    }


def position():
    return {
        "positionId": "87654", "symbol": "BTC_USDT",
        "positionType": 2, "openType": 1,
        "holdVol": 1, "leverage": 1,
    }


def stop():
    return {
        "id": "789", "orderId": "123456",
        "positionId": "87654", "symbol": "BTC_USDT",
        "state": 1, "isFinished": 0, "triggerSide": 0,
        "positionType": 2, "lossTrend": 1,
        "stopLossPrice": "75958.8",
        "volType": 1, "profitLossVolType": "SEPARATE",
        "stopLossVol": 1, "errorCode": 0,
        "stopLossType": 0, "stopLossReverse": 2,
    }


def terminal(*, state=3, side=2):
    r = stop()
    r.update(state=state, isFinished=1, triggerSide=side,
             placeOrderId="123457", errorCode=0)
    return r


def generated_close():
    return {
        "orderId": "123457", "positionId": "87654",
        "symbol": "BTC_USDT", "side": 2, "state": 3,
        "orderType": 5, "dealVol": 1,
    }


def counts(n):
    return {
        "sumCount": n, "stopOrderCount": n, "limitOrderCount": 0,
        "planOrderCount": 0, "trackOrderCount": 0,
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


def journal():
    return {
        "version": 1, "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": FP, "symbol": "BTC_USDT", "external_oid": OID,
        "contracts": 1, "open_type": 1,
        "stop_price": "75958.8", "stop_bps": "5",
        "reference_last_price": "75920.8",
        "entry_post_attempted": True, "entry_ack_verified": True,
        "entry_order_id": "123456",
        "close_post_attempted": False, "close_oid": None,
        "close_ack_verified": False, "status": "ENTRY_ACKED",
    }


def fake_client():
    return type("Client", (), {
        "_private_get": AsyncMock(),
        "_public_get": AsyncMock(),
        "_private_post": AsyncMock(return_value={"orderId": "123456"}),
    })()


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    base = tmp_path / "T100WebPilot"
    monkeypatch.setattr(m, "BASE_DIR", base)
    monkeypatch.setattr(m, "JOURNAL", base / "tight_stop_trigger_001.json")
    return m.JOURNAL


def test_dedicated_journal_exclusive_durable_private(journal_path):
    m._write_first(journal())
    assert journal_path.stat().st_mode & 0o077 == 0
    assert journal_path.parent.stat().st_mode & 0o077 == 0
    assert m._load(FP) == journal()
    with pytest.raises(FileExistsError):
        m._write_first(journal())
    assert m._load(FP) == journal()


def test_journal_account_mismatch_and_symlink_fail(journal_path):
    m._write_first(journal())
    with pytest.raises(m.TriggerPilotHalted, match="ACCOUNT"):
        m._load("e" * 64)
    journal_path.unlink()
    data = journal_path.parent / "other"
    data.write_text("private")
    journal_path.symlink_to(data)
    with pytest.raises(m.TriggerPilotHalted, match="MISSING"):
        m._load(FP)


def test_exact_open_body_and_embedded_stop_but_no_tp():
    body = m._entry_body(preview(), OID)
    assert body == {
        "symbol": "BTC_USDT", "price": "75920.8", "vol": 1,
        "leverage": 1, "side": 3, "type": 5, "openType": 1,
        "positionMode": 1, "externalOid": OID,
        "stopLossPrice": "75958.8", "lossTrend": 1,
    }
    assert "takeProfitPrice" not in body


def test_five_basis_point_stop_is_tighter_than_prior_pilot():
    data = preview()
    last = m.D(data["reference_last_price"])
    stop = m.D(data["stop_loss_price"])
    bps = (stop / last - 1) * m.D(10000)
    assert m.STOP_BPS == m.D(5)
    assert m.MAX_BPS == m.D(7)
    assert m.D(5) <= bps <= m.D(7)
    assert stop - last < last * m.D("0.0015")
    assert m._entry_body(data, OID)["stopLossPrice"] == "75958.8"


def test_existing_15bp_journal_will_not_be_repurposed():
    old = journal()
    old["stop_bps"] = "15"
    assert old["stop_bps"] != "5"


def test_legacy_15bp_journal_can_be_read_without_reusing_for_entry(journal_path):
    older = journal()
    older["stop_bps"] = "15"
    older["stop_price"] = "76034.7"
    m._write_first(older)
    loaded = m._load(FP)
    assert loaded["stop_bps"] == "15"
    assert loaded["stop_price"] == "76034.7"
    # An old journal always fences any new 5bp --send, regardless of whether
    # the old HTTP outcome is known. Recovery must use GET or exact close.
    assert journal_path.exists()


@pytest.mark.asyncio
async def test_legacy_15bp_journal_status_reads_original_stop_only(
    journal_path,
):
    older = journal()
    older["stop_bps"] = "15"
    older["stop_price"] = "76034.7"
    m._write_first(older)
    client = fake_client()
    s = stop()
    s["stopLossPrice"] = "76034.7"
    client._private_get.side_effect = [
        entry(), [position()], [s], counts(1), [],
    ]
    result = await m._status(client, FP)
    assert result["status"] == "ONE_SHORT_WITH_ATTACHED_SL_FIELDS_OBSERVED_NONCERTIFYING"
    assert result["journal_original_stop_bps"] == "15"
    assert result["journal_is_legacy_15bp"] is True
    assert result["stop_count"] == 1
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_15bp_existing_journal_blocks_new_5bp_post(journal_path):
    older = journal()
    older["stop_bps"] = "15"
    older["stop_price"] = "76034.7"
    m._write_first(older)
    client = fake_client()
    with pytest.raises(m.TriggerPilotHalted, match="ALREADY_JOURNALED"):
        await m._send(client, FP)
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


def test_main_reports_allowlisted_dry_run_reason_without_secret(capsys):
    with (
        patch.object(m, "OneShotProcessLock"),
        patch.object(m.asyncio, "run", side_effect=m.PilotDryRunHalted(
            "NEW_SUBACCOUNT_CURRENT_INVENTORY_NOT_FLAT",
        )),
        patch.object(m.sys, "argv", [
            "t100_web_pilot_tight_stop_trigger_cli", "--check",
        ]),
    ):
        with pytest.raises(SystemExit) as err:
            m.main()
    assert err.value.code == 2
    output = capsys.readouterr().out
    assert "PILOT_HALTED=NEW_SUBACCOUNT_CURRENT_INVENTORY_NOT_FLAT" in output
    assert "DO_NOT_REPEAT" not in output


def test_reject_other_oid():
    with pytest.raises(m.TriggerPilotHalted, match="ORIGIN"):
        m._entry_body(preview(), "BAD")


@pytest.mark.asyncio
async def test_realistic_preview_recomputes_15bps_from_contract_tick():
    client = fake_client()
    raw = {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "notional_le_25_pct_observed_available": True,
        "contracts": 1, "margin": "ISOLATED",
        "leverage": "1x", "entry_allowed": False,
        "live_authorization": False, "mexc_orders_sent": 0,
        "reference_last_price": "75920.8",
        "indicative_notional_usdt": "7.5921",
    }
    client._public_get.side_effect = [
        {"symbol": "BTC_USDT", "priceUnit": "0.1"},
        {"symbol": "BTC_USDT", "lastPrice": "75920.8",
         "ask1": "75922", "timestamp": int(m.time.time() * 1000)},
    ]
    with patch.object(m, "preflight", new=AsyncMock(return_value=raw)):
        result = await m._preview(client)
    assert result["status"] == "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED"
    assert result["stop_loss_price"] == "75958.8"
    assert result["stop_bps"] == "5"
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_notional_blocks_before_real_order():
    client = fake_client()
    raw = {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "notional_le_25_pct_observed_available": True,
        "contracts": 1, "margin": "ISOLATED",
        "leverage": "1x", "entry_allowed": False,
        "live_authorization": False, "mexc_orders_sent": 0,
        "reference_last_price": "75920.8",
        "indicative_notional_usdt": "13.01",
    }
    client._public_get.side_effect = [
        {"symbol": "BTC_USDT", "priceUnit": "0.1"},
        {"symbol": "BTC_USDT", "lastPrice": "75920.8",
         "ask1": "75922", "timestamp": int(m.time.time() * 1000)},
    ]
    with patch.object(m, "preflight", new=AsyncMock(return_value=raw)):
        with pytest.raises(m.TriggerPilotHalted, match="NOTIONAL"):
            await m._preview(client)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_entry_post_with_fsync_fenced_journal(journal_path):
    client = fake_client()
    async def post(path, body, **kw):
        assert journal_path.exists()
        assert m._load(FP)["entry_post_attempted"]
        assert not m._load(FP)["entry_ack_verified"]
        return {"orderId": "123456"}
    client._private_post.side_effect = post
    with (
        patch.object(m, "_preview", new=AsyncMock(return_value=preview())),
        patch.object(m, "_confirm"),
        patch.object(m, "_status", new=AsyncMock(return_value={"status": "OPEN"})),
        patch.object(m.secrets, "token_hex", return_value="a" * 24),
    ):
        await m._send(client, FP)
        assert client._private_post.await_count == 1
        assert client._private_post.await_args.args[0] == m.OPEN_PATH
        assert client._private_post.await_args.args[1]["stopLossPrice"] == "75958.8"
        assert m._load(FP)["entry_ack_verified"] is True
        with pytest.raises(m.TriggerPilotHalted, match="ALREADY_JOURNALED"):
            await m._send(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_uncertain_entry_post_cannot_repeat(journal_path):
    client = fake_client()
    client._private_post.side_effect = TimeoutError("network uncertain")
    with (
        patch.object(m, "_preview", new=AsyncMock(return_value=preview())),
        patch.object(m, "_confirm"),
        patch.object(m.secrets, "token_hex", return_value="a" * 24),
    ):
        with pytest.raises(TimeoutError):
            await m._send(client, FP)
        assert m._load(FP)["entry_post_attempted"]
        assert not m._load(FP)["entry_ack_verified"]
        with pytest.raises(m.TriggerPilotHalted, match="ALREADY_JOURNALED"):
            await m._send(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_confirmation_declined_never_journals_or_sends(journal_path):
    client = fake_client()
    with (
        patch.object(m, "_preview", new=AsyncMock(return_value=preview())),
        patch.object(m, "_confirm", side_effect=m.TriggerPilotHalted("DECLINED")),
    ):
        with pytest.raises(m.TriggerPilotHalted):
            await m._send(client, FP)
    assert not journal_path.exists()
    client._private_post.assert_not_awaited()


def test_partial_separate_leg_qty_1_qualifies_not_market_cert():
    result = m._open_stop(stop(), pid="87654", oid="123456", price=m.D("75958.8"))
    assert result["active_attached_sl_quantity_and_price_observed"]
    assert result["origin_is_order_attached"]
    assert result["stop_loss_type"] == "MARKET"
    assert not result["sl_execution_verified"]


@pytest.mark.parametrize("field,value", [
    ("stopLossVol", "0.5"), ("stopLossPrice", "75959"),
    ("positionId", "2"), ("orderId", "4"), ("state", 5),
])
def test_open_stop_identity_or_quantity_changes_fail(field, value):
    s = stop()
    s[field] = value
    result = m._open_stop(s, pid="87654", oid="123456", price=m.D("75958.8"))
    assert not result["active_attached_sl_quantity_and_price_observed"]


@pytest.mark.asyncio
async def test_open_status_no_exchange_post(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [entry(), [position()], [stop()], counts(1), []]
    report = await m._status(client, FP)
    assert report["status"] == "ONE_SHORT_WITH_ATTACHED_SL_FIELDS_OBSERVED_NONCERTIFYING"
    assert report["position_count"] == 1 and report["stop_count"] == 1
    assert not report["actual_sl_trigger_and_market_close_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_stop_trigger_and_order_fill_required_for_noncertifying_success(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [
        entry(), [], [], counts(0), [terminal()], generated_close(),
    ]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())) as inventory:
        report = await m._status(client, FP)
    assert report["actual_sl_trigger_and_market_close_verified"]
    assert report["status"] == "EXACT_SL_TRIGGER_AND_MARKET_CLOSE_FILL_VERIFIED_NONAUTHORIZING"
    assert report["account_stably_flat_verified"]
    assert inventory.await_count == 2
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_canceled_stop_and_flat_not_mistaken_for_trigger(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [entry(), [], [], counts(0), [terminal(state=2,side=0)]]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())):
        report = await m._status(client, FP)
    assert report["status"] == "FLAT_NOT_VENUE_VERIFIED_SL_TRIGGER"
    assert report["finished_stop_state"] == "CANCELED"
    assert not report["actual_sl_trigger_and_market_close_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_untriggered_audit_explains_exact_boolean_failure_without_raw_id(
    journal_path,
):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [
        entry(), [], [], counts(0), [terminal(state=2, side=0)],
    ]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())):
        result = await m._status(client, FP)
    assert result["status"] == "FLAT_NOT_VENUE_VERIFIED_SL_TRIGGER"
    assert result["finished_stop_state"] == "CANCELED"
    assert result["finished_trigger_side"] == "NOT_TRIGGERED"
    assert result["historical_stop_state_field"] == 2
    assert result["historical_trigger_side_field"] == 0
    assert result["historical_stop_price_matches_journal"] is True
    assert result["historical_stop_generated_order_id_present"] is True
    assert result["historical_stop_error_code_status"] == "ZERO_NORMAL"
    assert result["historical_stop_terminal_flag_verified"] is True
    assert result["historical_stop_separate_leg_quantity_matches"] is True
    assert result["actual_sl_trigger_and_market_close_verified"] is False
    assert "123457" not in str(result)
    assert "760" not in str(result)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_string_encoded_stop_states_are_not_accepted_as_native_execution(
    journal_path,
):
    m._write_first(journal())
    client = fake_client()
    row = terminal()
    row["state"] = "3"
    row["triggerSide"] = "2"
    client._private_get.side_effect = [entry(), [], [], counts(0), [row]]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())):
        result = await m._status(client, FP)
    assert result["status"] == "FLAT_NOT_VENUE_VERIFIED_SL_TRIGGER"
    assert result["historical_state_field_type"] == "STRING"
    assert result["historical_trigger_field_type"] == "STRING"
    assert result["historical_stop_state_field"] == "NOT_REPORTED_OR_UNRECOGNIZED"
    assert result["historical_trigger_side_field"] == "NOT_REPORTED_OR_UNRECOGNIZED"
    assert not result["actual_sl_trigger_and_market_close_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_explicit_nonzero_stop_error_code_but_does_not_certify(
    journal_path,
):
    m._write_first(journal())
    client = fake_client()
    row = terminal(state=2, side=0)
    row["errorCode"] = 8912
    client._private_get.side_effect = [entry(), [], [], counts(0), [row]]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())):
        result = await m._status(client, FP)
    assert result["historical_stop_error_code_status"] == "NONZERO_REPORTED"
    assert not result["actual_sl_trigger_and_market_close_verified"]
    assert not result["live_authorization"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_without_filled_market_close_is_not_success(journal_path):
    m._write_first(journal())
    client = fake_client()
    bad = generated_close()
    bad["state"] = 2
    client._private_get.side_effect = [entry(), [], [], counts(0), [terminal()], bad]
    with patch.object(m, "run_inventory", new=AsyncMock(return_value=flat())):
        report = await m._status(client, FP)
    assert report["status"] == "SL_TRIGGER_SEEN_BUT_NATIVE_MARKET_CLOSE_FILL_UNVERIFIED"
    assert not report["actual_sl_trigger_and_market_close_verified"]


@pytest.mark.asyncio
async def test_get_only_wrapper_never_exposes_post():
    client = fake_client()
    safe = m._GetOnly(client)
    assert not hasattr(safe, "_private_post")
    with pytest.raises(m.TriggerPilotHalted):
        await safe.get("/api/v1/private/order/create")
    with pytest.raises(m.TriggerPilotHalted):
        await safe.get("/api/v1/private/stoporder/list/orders",
                       {"page_num": 500})
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_one_post_fenced_and_never_repeat(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [entry(), [position()], [position()]]
    client._private_post.return_value = {"orderId": "123457"}
    with (
        patch.object(m, "_confirm"),
        patch.object(m.secrets, "token_hex", return_value="b" * 24),
    ):
        await m._close(client, FP)
    j = m._load(FP)
    assert j["close_post_attempted"] and j["close_ack_verified"]
    assert client._private_post.await_count == 1
    body = client._private_post.await_args.args[1]
    assert body["side"] == 2 and body["type"] == 5 and body["vol"] == 1
    assert body["positionId"] == 87654
    with pytest.raises(m.TriggerPilotHalted, match="NO_RETRY"):
        await m._close(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_close_uncertain_no_retry(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [entry(), [position()], [position()]]
    client._private_post.side_effect = TimeoutError("unknown")
    with patch.object(m, "_confirm"):
        with pytest.raises(TimeoutError):
            await m._close(client, FP)
    assert m._load(FP)["close_post_attempted"]
    assert not m._load(FP)["close_ack_verified"]
    with pytest.raises(m.TriggerPilotHalted, match="NO_RETRY"):
        await m._close(client, FP)
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_no_second_close_if_short_already_flat(journal_path):
    m._write_first(journal())
    client = fake_client()
    client._private_get.side_effect = [entry(), []]
    with pytest.raises(m.TriggerPilotHalted, match="NOT_OPEN"):
        await m._close(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_watch_read_only_finishes_on_evidenced_trigger():
    client = fake_client()
    with patch.object(m, "_status", new=AsyncMock(return_value={
        "status": "EXACT_SL_TRIGGER_AND_MARKET_CLOSE_FILL_VERIFIED_NONAUTHORIZING",
        "position_count": 0, "stop_count": 0,
        "actual_sl_trigger_and_market_close_verified": True,
    })):
        await m._watch(client, FP)
    client._private_post.assert_not_awaited()



@pytest.mark.parametrize("field,value", [
    ("stopLossType", 1), ("errorCode", 51), ("stopLossReverse", 1),
])
def test_active_stop_with_conflicting_native_fields_is_unverified(field,value):
    row = stop()
    row[field] = value
    result = m._open_stop(row,pid="87654",oid="123456",price=m.D("75958.8"))
    assert not result["active_attached_sl_quantity_and_price_observed"]


@pytest.mark.asyncio
async def test_triggered_row_with_error_cannot_be_verified(journal_path):
    m._write_first(journal())
    client = fake_client()
    bad_stop = terminal()
    bad_stop["errorCode"] = 8912
    client._private_get.side_effect = [entry(), [], [], counts(0), [bad_stop]]
    with patch.object(m,"run_inventory",new=AsyncMock(return_value=flat())):
        result = await m._status(client,FP)
    assert result["status"] == "SL_TRIGGER_SEEN_LINKED_CLOSE_ID_OR_FIELDS_MISSING"
    assert not result["actual_sl_trigger_and_market_close_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_status_with_unrelated_limit_order_fails_closed(journal_path):
    m._write_first(journal())
    client = fake_client()
    extra = counts(1)
    extra["sumCount"] = 2
    extra["limitOrderCount"] = 1
    client._private_get.side_effect = [entry(), [position()], [stop()], extra, []]
    result = await m._status(client, FP)
    assert result["status"] == "UNRELATED_PENDING_ORDERS_CHECK_UI_NOW"
    assert not result["actual_sl_trigger_and_market_close_verified"]
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_order_ack_identity_drift_halts(journal_path):
    m._write_first(journal())
    client = fake_client()
    changed = entry()
    changed["orderId"] = "5566"
    client._private_get.return_value = changed
    with pytest.raises(m.TriggerPilotHalted, match="JOURNALED_ENTRY"):
        await m._status(client,FP)
    client._private_post.assert_not_awaited()



@pytest.mark.asyncio
async def test_quote_ask_too_close_to_sl_fails_before_post():
    client = fake_client()
    raw = {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "notional_le_25_pct_observed_available": True,
        "contracts": 1, "margin": "ISOLATED", "leverage": "1x",
        "entry_allowed": False, "live_authorization": False,
        "mexc_orders_sent": 0,
        "reference_last_price": "75920.8",
        "indicative_notional_usdt": "7.5921",
    }
    client._public_get.side_effect = [
        {"symbol": "BTC_USDT", "priceUnit": "0.1"},
        {"symbol": "BTC_USDT", "lastPrice": "75920.8",
         "ask1": "76012", "timestamp": int(m.time.time() * 1000)},
    ]
    with patch.object(m, "preflight", new=AsyncMock(return_value=raw)):
        with pytest.raises(m.TriggerPilotHalted, match="TICK_OR_MARKET"):
            await m._preview(client)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_market_quote_staleness_blocks_live_send():
    client = fake_client()
    raw = {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "notional_le_25_pct_observed_available": True,
        "contracts": 1, "margin": "ISOLATED", "leverage": "1x",
        "entry_allowed": False, "live_authorization": False,
        "mexc_orders_sent": 0,
        "reference_last_price": "75920.8",
        "indicative_notional_usdt": "7.5921",
    }
    client._public_get.side_effect = [
        {"symbol": "BTC_USDT", "priceUnit": "0.1"},
        {"symbol": "BTC_USDT", "lastPrice": "75920.8",
         "ask1": "75922", "timestamp": int(m.time.time() * 1000) - 10000},
    ]
    with patch.object(m, "preflight", new=AsyncMock(return_value=raw)):
        with pytest.raises(m.TriggerPilotHalted, match="SECOND_MARKET_QUOTE_STALE"):
            await m._preview(client)
    client._private_post.assert_not_awaited()
