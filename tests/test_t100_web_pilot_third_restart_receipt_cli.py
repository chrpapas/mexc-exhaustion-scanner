"""Local receipt durability and real-venue replay mechanics, NO MEXC POST."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app import t100_web_pilot_third_restart_receipt_cli as receipt
from app import t100_web_pilot_third_close_audit_cli as audit
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_tight_stop_trigger_cli as pilot

FINGERPRINT = "f" * 64
SECRET = "local-only-test-secret-never-production"
ORIGIN = "TPWEB" + "a" * 24


def close_evidence():
    return {
        "close_order_id": "1234567", "position_id": "456789",
        "close_external_oid": "MANUAL_OR_EXTERNAL",
        "exact_close_market_order_type": 5,
        "close_deals": [{
            "deal_id": "98765", "order_id": "1234567",
            "qty": "1", "fill_price": "75123.4",
            "timestamp_ms": 1794050000000, "taker": True,
        }],
        "entry_order_id": "111111",
        "entry_external_oid": ORIGIN,
        "entry_original_stop_price": "75375.0",
        "entry_contracts": "1",
        "account_fingerprint_sha256": FINGERPRINT,
        "journal_amend_post_attempted": False,
        "journal_close_post_attempted": False,
    }


def close_report():
    return {
        "status": audit.VERIFIED_CLOSE
        if hasattr(audit, "VERIFIED_CLOSE") else
        "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING",
        "exact_close_trade_deals_filled": True,
        "exact_close_order_filled": True,
        "close_market_order_type_verified": True,
        "close_api_error": "ZERO",
        "account_bookended_flat_verified": True,
        "filled_close_candidate_count": 1,
        "all_historical_orders_complete": False,
        "live_authorization": False,
    }


@pytest.fixture
def filesystem(tmp_path, monkeypatch):
    secure = tmp_path / "t100-owner-private"
    secure.mkdir(mode=0o700)
    os.chmod(secure, 0o700)
    journal = secure / "native_stop_amendment_001.json"
    journal.write_text(json.dumps({
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "entry_external_oid": ORIGIN,
        "stop_bps": "50",
    }), encoding="utf-8")
    os.chmod(journal, 0o600)
    monkeypatch.setattr(native, "THIRD_JOURNAL", journal)
    target = secure / "native_stop_amendment_001.closed_receipt.json"
    monkeypatch.setattr(receipt, "RECEIPT", target)
    return journal, target


def mocked_exchange():
    client = type("Client", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(),
    })()
    async def fresh_get(_client, _fingerprint, *, evidence_sink=None):
        assert evidence_sink is not None
        evidence_sink.update(copy.deepcopy(close_evidence()))
        return close_report()
    return client, AsyncMock(side_effect=fresh_get)


@pytest.mark.asyncio
async def test_seal_one_real_audit_then_new_process_replay_same_exact_evidence(
    filesystem,
):
    journal, saved = filesystem
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        first = await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        assert first["status"] == (
            "THIRD_CLOSED_PILOT_RECEIPT_SEALED_FRESH_REPLAY_REQUIRED"
        )
        assert first["restarted_process_replay_verified"] is False
        assert first["new_local_receipt_created"] is True
        assert first["source_historical_archive_complete"] is False
        assert saved.is_file()
        assert saved.stat().st_mode & 0o077 == 0
        saved_bytes = saved.read_bytes()
        journal_bytes = journal.read_bytes()
        assert b"1234567" not in saved_bytes
        assert b"75123.4" not in saved_bytes
        assert SECRET.encode() not in saved_bytes
        # No in-memory durable state: new execute() loads receipt from disk
        # and queries the exchange a second time.
        second = await receipt.execute(
            "replay", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
    assert second["status"] == (
        "THIRD_CLOSED_PILOT_RESTART_REPLAY_VERIFIED_NONAUTHORIZING"
    )
    assert second["restarted_process_replay_verified"] is True
    assert second["receipt_matches_fresh_mexc_get"] is True
    assert second["nonflat_restart_recovery_certified"] is False
    assert second["native_sl_trigger_origin_certified"] is False
    assert second["live_authorization"] is False
    assert second["mexc_orders_sent"] == 0
    assert saved.read_bytes() == saved_bytes
    assert journal.read_bytes() == journal_bytes
    assert auditor.await_count == 2
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_seal_no_exchange_calls_no_overwrite(filesystem):
    _, saved = filesystem
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        first = saved.read_bytes()
        with pytest.raises(
            pilot.TriggerPilotHalted, match="ALREADY_EXISTS_NO_REPLACE"
        ):
            await receipt.execute(
                "seal", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    assert saved.read_bytes() == first
    assert auditor.await_count == 1
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_venue_price_or_fill_change_rejected(filesystem):
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
    changed = close_evidence()
    changed["close_deals"][0]["fill_price"] = "75123.5"
    async def modified(_client, _fp, *, evidence_sink=None):
        evidence_sink.update(changed)
        return close_report()
    with patch.object(
        audit, "audit_third_pilot", new=AsyncMock(side_effect=modified)
    ):
        with pytest.raises(
            pilot.TriggerPilotHalted, match="EXCHANGE_EVIDENCE_CHANGED"
        ):
            await receipt.execute(
                "replay", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_wrong_api_secret_fails_not_false_success(filesystem):
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        with pytest.raises(
            pilot.TriggerPilotHalted, match="EXCHANGE_EVIDENCE_CHANGED"
        ):
            await receipt.execute(
                "replay", client=client, api_secret="another-api-secret",
                account_fingerprint=FINGERPRINT,
            )
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_modified_original_journal_fails_before_any_exchange_get(
    filesystem,
):
    journal, _ = filesystem
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        journal.write_text(journal.read_text() + "\n", encoding="utf-8")
        with pytest.raises(
            pilot.TriggerPilotHalted, match="JOURNAL_OR_ACCOUNT_DRIFT"
        ):
            await receipt.execute(
                "replay", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    assert auditor.await_count == 1


@pytest.mark.asyncio
async def test_replay_wrong_account_blocked_before_exchange_call(filesystem):
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        with pytest.raises(
            pilot.TriggerPilotHalted, match="JOURNAL_OR_ACCOUNT_DRIFT"
        ):
            await receipt.execute(
                "replay", client=client, api_secret=SECRET,
                account_fingerprint="0" * 64,
            )
    assert auditor.await_count == 1


@pytest.mark.asyncio
async def test_original_close_no_longer_verified_does_not_seal(filesystem):
    _, path = filesystem
    client = type("Client", (), {
        "_private_get": AsyncMock(),
        "_private_post": AsyncMock(),
    })()
    bad = close_report()
    bad["status"] = "ACCOUNT_NOT_FLAT_CLOSE_AUDIT_HALTED"
    with patch.object(
        audit, "audit_third_pilot", new=AsyncMock(return_value=bad)
    ):
        with pytest.raises(
            pilot.TriggerPilotHalted,
            match="REAL_EXCHANGE_REPLAY_UNVERIFIED",
        ):
            await receipt.execute(
                "seal", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    assert not path.exists()
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_market_type_or_api_zero_cannot_seal(filesystem):
    client, _ = mocked_exchange()
    bad = close_report()
    bad["close_market_order_type_verified"] = False
    async def outcome(_client, _fp, *, evidence_sink=None):
        evidence_sink.update(close_evidence())
        return bad
    with patch.object(
        audit, "audit_third_pilot", new=AsyncMock(side_effect=outcome)
    ):
        with pytest.raises(
            pilot.TriggerPilotHalted, match="REAL_EXCHANGE_REPLAY_UNVERIFIED"
        ):
            await receipt.execute(
                "seal", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    client._private_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsafe_receipt_permissions_rejected_on_replay(filesystem):
    _, path = filesystem
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        await receipt.execute(
            "seal", client=client, api_secret=SECRET,
            account_fingerprint=FINGERPRINT,
        )
        os.chmod(path, 0o644)
        with pytest.raises(
            pilot.TriggerPilotHalted, match="NOT_FOUND_OR_UNSAFE"
        ):
            await receipt.execute(
                "replay", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    assert auditor.await_count == 1


@pytest.mark.asyncio
async def test_receipt_symlink_rejected_before_any_exchange_read(
    filesystem, tmp_path,
):
    _, path = filesystem
    third_party = tmp_path / "fake-receipt.json"
    third_party.write_text("{}")
    path.symlink_to(third_party)
    client, auditor = mocked_exchange()
    with patch.object(audit, "audit_third_pilot", auditor):
        with pytest.raises(
            pilot.TriggerPilotHalted, match="ALREADY_EXISTS_NO_REPLACE"
        ):
            await receipt.execute(
                "seal", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
        with pytest.raises(
            pilot.TriggerPilotHalted, match="NOT_FOUND_OR_UNSAFE"
        ):
            await receipt.execute(
                "replay", client=client, api_secret=SECRET,
                account_fingerprint=FINGERPRINT,
            )
    auditor.assert_not_awaited()


def test_evidence_hmac_binds_actual_trade_details_not_only_report():
    first = close_evidence()
    digest = "f" * 64
    tag = receipt._commitment(
        api_secret=SECRET, account_fingerprint=FINGERPRINT,
        journal_digest=digest, evidence=first,
    )
    changed = copy.deepcopy(first)
    changed["close_deals"][0]["deal_id"] = "99999"
    assert receipt._commitment(
        api_secret=SECRET, account_fingerprint=FINGERPRINT,
        journal_digest=digest, evidence=changed,
    ) != tag


def test_read_receipt_refuses_extra_fields_and_bad_mac_shape(filesystem):
    _, path = filesystem
    record = receipt._new_receipt(
        account_fingerprint=FINGERPRINT,
        journal_digest="a" * 64,
        commitment="b" * 64,
    )
    record["totally_unexpected"] = True
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    with pytest.raises(pilot.TriggerPilotHalted, match="SHAPE_UNVERIFIED"):
        receipt._read_receipt()
