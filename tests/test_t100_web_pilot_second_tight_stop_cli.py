"""Offline no-order tests for second dedicated T100 5bp real pilot handoff."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_second_tight_stop_cli as second

FP = "a" * 64
ENTRY_OID = "TPWEB" + "b" * 24
CLOSE_OID = "TPWEB" + "c" * 24


def previous_journal():
    return {
        "version": 1,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": FP,
        "symbol": "BTC_USDT",
        "external_oid": ENTRY_OID,
        "contracts": 1, "open_type": 1,
        "stop_price": "76034.7", "stop_bps": "15",
        "reference_last_price": "75920.8",
        "entry_post_attempted": True,
        "entry_ack_verified": True,
        "entry_order_id": "11111",
        "close_post_attempted": True,
        "close_oid": CLOSE_OID,
        "close_ack_verified": True,
        "status": "CLOSE_ACKED_FLAT_NOT_YET_PROVEN",
    }


def entry():
    return {
        "orderId": "11111", "positionId": "12345",
        "externalOid": ENTRY_OID, "symbol": "BTC_USDT",
        "side": 3, "openType": 1, "state": 3,
        "dealVol": 1, "vol": 1,
    }


def close():
    return {
        "orderId": "22222", "positionId": "12345",
        "externalOid": CLOSE_OID, "symbol": "BTC_USDT",
        "side": 2, "openType": 1, "state": 3,
        "dealVol": 1, "vol": 1,
    }


def flat():
    return {
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": 0,
        "after_positions": 0,
        "before_current_tpsl": 0,
        "after_current_tpsl": 0,
        "reported_open_order_counts": {
            "sumCount": 0,
            "stopOrderCount": 0,
            "limitOrderCount": 0,
            "trackOrderCount": 0,
            "planOrderCount": 0,
        },
    }


def fake_client():
    return type("Client", (), {
        "_private_get": AsyncMock(side_effect=[entry(), close()]),
        "_private_post": AsyncMock(),
        "close": AsyncMock(),
    })()


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    root = tmp_path / "T100WebPilot"
    prior = root / "tight_stop_trigger_001.json"
    new = root / "tight_stop_trigger_002.json"
    monkeypatch.setattr(pilot, "BASE_DIR", root)
    monkeypatch.setattr(pilot, "JOURNAL", prior)
    monkeypatch.setattr(second, "PREVIOUS_JOURNAL", prior)
    monkeypatch.setattr(second, "SECOND_JOURNAL", new)
    pilot._write_first(previous_journal())
    return root, prior, new


@pytest.mark.asyncio
async def test_verified_one_contract_old_close_allows_new_preview(scoped):
    root, prior, new = scoped
    client = fake_client()
    with patch.object(second, "inventory_run",
                      new=AsyncMock(return_value=flat())) as inventory:
        result = await second.require_previous_close_verified(client, FP)
    assert result["previous_pilot_entry_and_close_fills_verified"]
    assert result["previous_pilot_account_flat_verified"]
    assert result["previous_stop_bps"] == "15"
    assert not result["second_pilot_journal_exists"]
    assert not result["entry_allowed"]
    assert not result["live_authorization"]
    assert inventory.await_count == 1
    assert client._private_get.await_count == 2
    client._private_post.assert_not_awaited()
    assert prior.is_file() and not new.exists()


@pytest.mark.asyncio
async def test_unclosed_previous_trade_blocks_new_entry(scoped):
    original = previous_journal()
    original["close_ack_verified"] = False
    pilot._persist(original)
    client = fake_client()
    with pytest.raises(pilot.TriggerPilotHalted, match="CLOSE_ACK_NOT_VERIFIED"):
        await second.require_previous_close_verified(client, FP)
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_ack_without_finished_close_order_blocks(scoped):
    client = fake_client()
    bad = close()
    bad["state"] = 2
    client._private_get.side_effect = [entry(), bad]
    with pytest.raises(pilot.TriggerPilotHalted, match="FULL_CLOSE_NOT_VERIFIED"):
        await second.require_previous_close_verified(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_order_or_position_id_drift_blocks(scoped):
    client = fake_client()
    bad = close()
    bad["positionId"] = "99999"
    client._private_get.side_effect = [entry(), bad]
    with pytest.raises(pilot.TriggerPilotHalted, match="FULL_CLOSE_NOT_VERIFIED"):
        await second.require_previous_close_verified(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_close_not_full_volume_blocks(scoped):
    client = fake_client()
    bad = close()
    bad["dealVol"] = "0.5"
    client._private_get.side_effect = [entry(), bad]
    with pytest.raises(pilot.TriggerPilotHalted, match="FULL_CLOSE_NOT_VERIFIED"):
        await second.require_previous_close_verified(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonflat_account_blocks_second_pilot(scoped):
    client = fake_client()
    unsettled = flat()
    unsettled["after_positions"] = 1
    with patch.object(second, "inventory_run",
                      new=AsyncMock(return_value=unsettled)):
        with pytest.raises(pilot.TriggerPilotHalted,
                           match="NOT_STABLY_FLAT"):
            await second.require_previous_close_verified(client, FP)
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_prior_credential_key_fails_before_orders(scoped):
    client = fake_client()
    with pytest.raises(pilot.TriggerPilotHalted,
                       match="JOURNAL_ACCOUNT_OR_SHAPE_MISMATCH"):
        await second.require_previous_close_verified(client, "d" * 64)
    client._private_get.assert_not_awaited()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_check_only_gets_and_uses_second_fresh_quote(scoped):
    root, old, new = scoped
    client = fake_client()
    with (
        patch.object(second.sys, "platform", "darwin"),
        patch.object(second, "_allow_real_mode_env"),
        patch.object(second, "read_pilot_keychain_credentials",
                     return_value=("mock-key", "mock-secret")),
        patch.object(second, "MexcTradeClient", return_value=client),
        patch.object(second, "inventory_run",
                     new=AsyncMock(return_value=flat())),
        patch.object(pilot, "_preview",
                     new=AsyncMock(return_value={"stop_bps": "5"})) as preview,
    ):
        fp = __import__("hashlib").sha256(b"mock-key").hexdigest()
        data = previous_journal()
        data["key_sha256"] = fp
        pilot._persist(data)
        result = await second.run("check")
    assert result["previous_pilot_account_flat_verified"]
    assert result["second_pilot_preview"] == {"stop_bps": "5"}
    assert not new.exists()
    assert pilot.JOURNAL == old
    assert preview.await_count == 1
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_send_calls_only_new_journal_after_prior_close(scoped):
    root, old, new = scoped
    client = fake_client()
    with (
        patch.object(second.sys, "platform", "darwin"),
        patch.object(second, "_allow_real_mode_env"),
        patch.object(second, "read_pilot_keychain_credentials",
                     return_value=("mock-key", "mock-secret")),
        patch.object(second, "MexcTradeClient", return_value=client),
        patch.object(second, "inventory_run",
                     new=AsyncMock(return_value=flat())),
        patch.object(pilot, "_send", new=AsyncMock()) as attempt,
    ):
        fp = __import__("hashlib").sha256(b"mock-key").hexdigest()
        original = previous_journal()
        original["key_sha256"] = fp
        pilot._persist(original)
        await second.run("send")
    assert attempt.await_count == 1
    assert pilot.JOURNAL == old
    assert not new.exists()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_journal_blocks_reentry_without_post(scoped):
    root, old, new = scoped
    new.write_text("already exists")
    client = fake_client()
    with (
        patch.object(second.sys, "platform", "darwin"),
        patch.object(second, "_allow_real_mode_env"),
        patch.object(second, "read_pilot_keychain_credentials",
                     return_value=("mock-key", "mock-secret")),
        patch.object(second, "MexcTradeClient", return_value=client),
        patch.object(second, "inventory_run",
                     new=AsyncMock(return_value=flat())),
        patch.object(pilot, "_send", new=AsyncMock()) as attempt,
    ):
        fp = __import__("hashlib").sha256(b"mock-key").hexdigest()
        original = previous_journal()
        original["key_sha256"] = fp
        pilot._persist(original)
        with pytest.raises(pilot.TriggerPilotHalted,
                           match="SECOND_PILOT_JOURNAL_EXISTS"):
            await second.run("send")
    attempt.assert_not_awaited()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_status_only_reads_second_journal(scoped):
    root, old, new = scoped
    client = fake_client()
    with (
        patch.object(second.sys, "platform", "darwin"),
        patch.object(second, "_allow_real_mode_env"),
        patch.object(second, "read_pilot_keychain_credentials",
                     return_value=("mock-key", "mock-secret")),
        patch.object(second, "MexcTradeClient", return_value=client),
        patch.object(pilot, "_status",
                     new=AsyncMock(return_value={"status": "GET_ONLY"})) as status,
    ):
        result = await second.run("status")
        assert result["status"] == "GET_ONLY"
        assert pilot.JOURNAL == new
    status.assert_awaited_once()
    assert pilot.JOURNAL == old
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_close_does_not_require_previous_recheck_if_second_is_open(
    scoped,
):
    root, old, new = scoped
    client = fake_client()
    with (
        patch.object(second.sys, "platform", "darwin"),
        patch.object(second, "_allow_real_mode_env"),
        patch.object(second, "read_pilot_keychain_credentials",
                     return_value=("mock-key", "mock-secret")),
        patch.object(second, "MexcTradeClient", return_value=client),
        patch.object(pilot, "_close", new=AsyncMock()) as close_attempt,
    ):
        await second.run("close")
        assert pilot.JOURNAL == new
    close_attempt.assert_awaited_once()
    assert pilot.JOURNAL == old
    client._private_post.assert_not_awaited()


def test_exact_prior_external_id_validation():
    assert second._valid_oid(ENTRY_OID)
    assert not second._valid_oid("TPWEBbad")
    assert not second._valid_oid("TPWEB" + "b" * 24 + "X")
