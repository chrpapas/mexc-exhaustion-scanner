"""Offline-only tests of the REAL T100 one-shot pilot's no-retry boundaries.

All exchange calls are fakes. No live account is accessed.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import t100_web_pilot_one_shot_cli as pilot

OID = "TPWEB" + "a" * 24


def preview():
    return {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "inventory_status": "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
        "symbol": "BTC_USDT", "direction": "SHORT", "contracts": 1,
        "margin": "ISOLATED", "leverage": "1x", "market_order": True,
        "stop_reference": "LATEST_PRICE", "notional_ceiling_usdt": "12.50",
        "available_futures_ge_12_50": True,
        "notional_le_25_pct_observed_available": True,
        "reference_last_price": "75875.2",
        "embedded_stop_loss": "76634.0",
        "indicative_notional_usdt": "7.5875",
        "entry_allowed": False, "live_authorization": False,
        "native_stop_protectiveness_verified": False,
        "exchange_atomicity_verified": False,
        "loss_cap_guaranteed": False, "mexc_orders_sent": 0,
    }


def fake_client():
    return type("FakeClient", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(return_value={"orderId": "1234"}),
    })()


@pytest.fixture
def temp_journal(tmp_path, monkeypatch):
    target = tmp_path / "state" / "one_contract_order.json"
    monkeypatch.setattr(pilot, "JOURNAL", target)
    return target


def journal_data():
    from app.t100_web_pilot_entry_review import draft_entry_request
    return {
        "version": 1, "symbol": "BTC_USDT", "key_sha256": "012345" * 10 + "0123",
        "external_oid": OID,
        "draft": draft_entry_request(preview(), external_oid=OID),
        "entry_post_attempted": True, "entry_ack_verified": False,
        "entry_order_id": None, "close_post_attempted": False,
        "close_oid": None, "close_ack_verified": False,
        "status": "ENTRY_POST_OUTCOME_UNKNOWN_RECONCILE_ONLY",
    }


def test_one_shot_journal_is_exclusive_durable_and_private(temp_journal):
    d = journal_data()
    pilot._first_journal(d)
    assert temp_journal.exists()
    assert temp_journal.stat().st_mode & 0o077 == 0
    assert temp_journal.parent.stat().st_mode & 0o077 == 0
    assert pilot._load_journal(d["key_sha256"]) == d
    with pytest.raises(FileExistsError):
        pilot._first_journal(d)
    assert pilot._load_journal(d["key_sha256"]) == d


def test_key_binding_and_symlink_are_rejected(temp_journal):
    d = journal_data()
    pilot._first_journal(d)
    with pytest.raises(pilot.OneShotHalted, match="ACCOUNT"):
        pilot._load_journal("f" * 64)
    target = temp_journal.parent / "other"
    target.write_text("secret")
    temp_journal.unlink()
    temp_journal.symlink_to(target)
    with pytest.raises(pilot.OneShotHalted, match="UNSAFE"):
        pilot._load_journal(d["key_sha256"])


def test_lock_blocks_simultaneous_operator_process(temp_journal):
    with pilot.OneShotProcessLock():
        with pytest.raises(pilot.OneShotHalted, match="ANOTHER_PILOT_PROCESS"):
            with pilot.OneShotProcessLock():
                pass
    with pilot.OneShotProcessLock():
        pass


@pytest.mark.asyncio
async def test_entry_one_post_only_even_if_rerun_attempted(temp_journal):
    client = fake_client()
    first = {
        "status": "NATIVE_STOP_FIELDS_OBSERVED_STILL_NONCERTIFYING",
        "active_native_stop_fields_observed": True,
    }
    with (
        patch.object(pilot, "_check", new=AsyncMock(return_value=preview())),
        patch.object(pilot, "_confirmed"),
        patch.object(pilot, "_make_oid", return_value=OID),
        patch.object(pilot, "_snapshot", new=AsyncMock(return_value=first)),
    ):
        await pilot._send(client, "012345" * 10 + "0123")
        assert client._private_post.await_count == 1
        assert client._private_post.await_args.args[0] == pilot.ORDER_CREATE
        body = client._private_post.await_args.args[1]
        assert body["externalOid"] == OID
        assert body["side"] == 3 and body["vol"] == 1
        assert body["stopLossPrice"] == "76634.0"
        assert body["openType"] == 1 and body["leverage"] == 1
        data = pilot._load_journal("012345" * 10 + "0123")
        assert data["entry_ack_verified"]
        assert data["entry_order_id"] == "1234"
        with pytest.raises(pilot.OneShotHalted, match="NO_RETRY"):
            await pilot._send(client, "012345" * 10 + "0123")
        assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_entry_timeout_remains_journaled_and_prevents_resubmission(temp_journal):
    client = fake_client()
    client._private_post.side_effect = TimeoutError("NETWORK: secret data")
    with (
        patch.object(pilot, "_check", new=AsyncMock(return_value=preview())),
        patch.object(pilot, "_confirmed"),
        patch.object(pilot, "_make_oid", return_value=OID),
    ):
        with pytest.raises(TimeoutError):
            await pilot._send(client, "012345" * 10 + "0123")
        data = pilot._load_journal("012345" * 10 + "0123")
        assert data["entry_post_attempted"] and not data["entry_ack_verified"]
        with pytest.raises(pilot.OneShotHalted, match="NO_RETRY"):
            await pilot._send(client, "012345" * 10 + "0123")
        assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_failed_preflight_never_journals_or_posts(temp_journal):
    client = fake_client()
    with patch.object(pilot, "_check", new=AsyncMock(side_effect=RuntimeError("stop"))):
        with pytest.raises(RuntimeError):
            await pilot._send(client, "012345" * 10 + "0123")
    assert not temp_journal.exists()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_confirmation_price_move_blocks_without_post(temp_journal):
    client = fake_client()
    moved = preview()
    moved["reference_last_price"] = "78000"
    with (
        patch.object(pilot, "_check", new=AsyncMock(side_effect=[preview(), moved])),
        patch.object(pilot, "_confirmed"),
    ):
        with pytest.raises(pilot.OneShotHalted, match="PRICE_MOVED"):
            await pilot._send(client, "012345" * 10 + "0123")
    assert not temp_journal.exists()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_postflight_endpoint_allowlist_denies_mutations():
    client = fake_client()
    safe = pilot.PilotPostflightGetOnly(client)
    with pytest.raises(pilot.OneShotHalted):
        await safe.get("/api/v1/private/order/create")
    with pytest.raises(pilot.OneShotHalted):
        await safe.get("/api/v1/private/order/external/BTC_USDT/foo")
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


def order():
    return {
        "orderId": "555", "positionId": 777,
        "externalOid": OID, "symbol": "BTC_USDT",
        "side": 3, "state": 3, "dealVol": 1,
    }


def position():
    return {
        "positionId": 777, "symbol": "BTC_USDT",
        "positionType": 2, "openType": 1,
        "leverage": 1, "holdVol": 1, "unRealizedPnl": "0.005",
    }


def stored():
    d = journal_data()
    return d


@pytest.mark.asyncio
async def test_close_one_post_and_durable_no_retry_with_pnl_drift(temp_journal):
    d = stored()
    pilot._first_journal(d)
    client = fake_client()
    p2 = position()
    p2["unRealizedPnl"] = "0.007"
    client._private_get.side_effect = [order(), [position()], [p2]]
    with (
        patch.object(pilot, "_confirmed"),
        patch.object(pilot, "_make_oid", return_value="TPWEB" + "b" * 24),
    ):
        await pilot._close(client, d["key_sha256"])
    assert client._private_post.await_count == 1
    body = client._private_post.await_args.args[1]
    assert body["side"] == 2 and body["vol"] == 1
    assert body["positionId"] == 777 and body["openType"] == 1
    assert body["positionMode"] == 1
    after = pilot._load_journal(d["key_sha256"])
    assert after["close_post_attempted"] and after["close_ack_verified"]
    with pytest.raises(pilot.OneShotHalted, match="NO_RETRY"):
        await pilot._close(client, d["key_sha256"])
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_close_unknown_post_does_not_retry(temp_journal):
    d = stored()
    pilot._first_journal(d)
    client = fake_client()
    client._private_get.side_effect = [order(), [position()], [position()]]
    client._private_post.side_effect = TimeoutError("unknown")
    with patch.object(pilot, "_confirmed"):
        with pytest.raises(TimeoutError):
            await pilot._close(client, d["key_sha256"])
    after = pilot._load_journal(d["key_sha256"])
    assert after["close_post_attempted"] and not after["close_ack_verified"]
    with pytest.raises(pilot.OneShotHalted, match="NO_RETRY"):
        await pilot._close(client, d["key_sha256"])
    assert client._private_post.await_count == 1


@pytest.mark.asyncio
async def test_close_rejects_unrelated_position_no_post(temp_journal):
    d = stored()
    pilot._first_journal(d)
    client = fake_client()
    wrong = position()
    wrong["positionId"] = 999
    client._private_get.side_effect = [order(), [wrong]]
    with pytest.raises(pilot.OneShotHalted, match="OWNED_SHORT"):
        await pilot._close(client, d["key_sha256"])
    client._private_post.assert_not_awaited()
    assert not pilot._load_journal(d["key_sha256"])["close_post_attempted"]


@pytest.mark.asyncio
async def test_status_is_read_only_and_uses_journal_scope(temp_journal):
    d = stored()
    pilot._first_journal(d)
    client = fake_client()
    with patch.object(pilot, "_snapshot", new=AsyncMock(return_value={"status": "UNKNOWN"})):
        await pilot._status(client, d["key_sha256"])
    client._private_post.assert_not_awaited()
