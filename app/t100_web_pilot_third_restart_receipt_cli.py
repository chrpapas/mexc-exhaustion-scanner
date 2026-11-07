"""Durable closed-pilot recovery receipt, signed GET-only MEXC replay.

--seal: independently re-audit the real third one-contract close, then
        *locally* create ONE 0600 HMAC-SHA256 receipt bound to actual venue
        entry/exit/trade identifiers (which never leave process output),
        journal bytes, and the existing T100 WEB-pilot API credential.
--replay: from a NEW process, verify that receipt + original journal bytes,
          then independently re-query real MEXC history, fills and two flat
          inventory bookends; compare the private evidence commitment.

Both actions are GET-only at MEXC. --seal writes ONLY a new local receipt,
never the original journal, database, Render, or BOXWAVE. --replay does
not write any file. Neither proves nonflat crash recovery, native stop
modification, full account-history pagination, stop-trigger provenance or
production readiness. Never print raw IDs, prices, API keys, or receipt HMAC.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
from typing import Any

from app import t100_web_pilot_third_close_audit_cli as closed
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app.t100_live_current_inventory_cli import read_pilot_keychain_credentials
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

RECEIPT_VERSION = 1
RECEIPT = native.THIRD_JOURNAL.with_name(
    "native_stop_amendment_001.closed_receipt.json"
)
DOMAIN = b"T100-CLOSED-THIRD-RESTART-REPLAY-V1\x00"
VERIFIED_CLOSE = (
    "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
)


def _secure_journal_digest() -> str:
    path = native.THIRD_JOURNAL
    if (
        path.is_symlink() or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_JOURNAL_PATH_UNSAFE")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(
    *,
    account_fingerprint: str,
    journal_digest: str,
    evidence: dict[str, Any],
) -> bytes:
    if (
        type(evidence) is not dict
        or evidence.get("account_fingerprint_sha256") != account_fingerprint
        or evidence.get("entry_contracts") != "1"
        or type(evidence.get("close_deals")) is not list
        or not evidence["close_deals"]
        or not evidence.get("entry_order_id")
        or not evidence.get("close_order_id")
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_EXACT_EVIDENCE_UNVERIFIED")
    return DOMAIN + json.dumps(
        {
            "version": RECEIPT_VERSION,
            "account_fingerprint_sha256": account_fingerprint,
            "journal_sha256": journal_digest,
            "actual_verified_exchange_evidence": evidence,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _commitment(
    *,
    api_secret: str,
    account_fingerprint: str,
    journal_digest: str,
    evidence: dict[str, Any],
) -> str:
    return hmac.new(
        api_secret.encode("utf-8"),
        _canonical_bytes(
            account_fingerprint=account_fingerprint,
            journal_digest=journal_digest,
            evidence=evidence,
        ), hashlib.sha256,
    ).hexdigest()


def _new_receipt(
    *,
    account_fingerprint: str, journal_digest: str,
    commitment: str,
) -> dict[str, Any]:
    return {
        "version": RECEIPT_VERSION,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_fingerprint_sha256": account_fingerprint,
        "journal_sha256": journal_digest,
        "evidence_hmac_sha256": commitment,
        "close_recovery_only_not_nonflat": True,
        "native_sl_trigger_origin_certified": False,
        "native_amendment_live_certified": False,
        "live_authorization": False,
    }


def _write_receipt_once(record: dict[str, Any]) -> None:
    path = RECEIPT
    native.THIRD_JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        native.THIRD_JOURNAL.parent.is_symlink()
        or native.THIRD_JOURNAL.parent.stat().st_mode & 0o077
        or path.parent.resolve() != native.THIRD_JOURNAL.parent.resolve()
        or path == native.THIRD_JOURNAL
        or path.is_symlink()
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_PATH_UNSAFE")
    if path.exists():
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_ALREADY_EXISTS_NO_REPLACE")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        # The file is an incomplete or uncertain local artifact. Keep it,
        # refuse overwrites. Operator investigation is required.
        raise


def _read_receipt() -> dict[str, Any]:
    path = RECEIPT
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
        or path.stat().st_size > 4096
        or path.stat().st_size <= 0
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_NOT_FOUND_OR_UNSAFE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_JSON_UNVERIFIED") from exc
    if (
        type(value) is not dict or set(value) != {
            "version", "credential_scope",
            "account_fingerprint_sha256", "journal_sha256",
            "evidence_hmac_sha256", "close_recovery_only_not_nonflat",
            "native_sl_trigger_origin_certified",
            "native_amendment_live_certified", "live_authorization",
        }
        or value.get("version") != RECEIPT_VERSION
        or value.get("credential_scope") != "T100_WEB_PILOT_SUBACCOUNT"
        or type(value.get("account_fingerprint_sha256")) is not str
        or type(value.get("journal_sha256")) is not str
        or type(value.get("evidence_hmac_sha256")) is not str
        or any(
            len(value[k]) != 64
            or any(x not in "0123456789abcdef" for x in value[k])
            for k in (
                "account_fingerprint_sha256", "journal_sha256",
                "evidence_hmac_sha256",
            )
        )
        or value.get("close_recovery_only_not_nonflat") is not True
        or value.get("native_sl_trigger_origin_certified") is not False
        or value.get("native_amendment_live_certified") is not False
        or value.get("live_authorization") is not False
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_SHAPE_UNVERIFIED")
    return value


async def execute(action: str, *, client: Any, api_secret: str,
                  account_fingerprint: str) -> dict[str, Any]:
    if action not in ("seal", "replay"):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_ACTION_UNRECOGNIZED")
    if action == "seal" and (RECEIPT.exists() or RECEIPT.is_symlink()):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_ALREADY_EXISTS_NO_REPLACE")
    prior = _read_receipt() if action == "replay" else None
    journal_before = _secure_journal_digest()
    if prior is not None and (
        prior["account_fingerprint_sha256"] != account_fingerprint
        or prior["journal_sha256"] != journal_before
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_JOURNAL_OR_ACCOUNT_DRIFT")
    evidence: dict[str, Any] = {}
    report = await closed.audit_third_pilot(
        client, account_fingerprint, evidence_sink=evidence,
    )
    journal_after = _secure_journal_digest()
    if journal_before != journal_after:
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_JOURNAL_CHANGED_DURING_GET")
    if (
        report.get("status") != VERIFIED_CLOSE
        or report.get("exact_close_trade_deals_filled") is not True
        or report.get("exact_close_order_filled") is not True
        or report.get("close_market_order_type_verified") is not True
        or report.get("close_api_error") != "ZERO"
        or report.get("account_bookended_flat_verified") is not True
        or report.get("filled_close_candidate_count") != 1
        or len(evidence) == 0
    ):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_REAL_EXCHANGE_REPLAY_UNVERIFIED")
    tag = _commitment(
        api_secret=api_secret,
        account_fingerprint=account_fingerprint,
        journal_digest=journal_before,
        evidence=evidence,
    )
    if action == "seal":
        _write_receipt_once(_new_receipt(
            account_fingerprint=account_fingerprint,
            journal_digest=journal_before,
            commitment=tag,
        ))
        return {
            "status": "THIRD_CLOSED_PILOT_RECEIPT_SEALED_FRESH_REPLAY_REQUIRED",
            "new_local_receipt_created": True,
            "receipt_matches_fresh_mexc_get": True,
            "restarted_process_replay_verified": False,
            "account_bookended_flat_verified": True,
            "source_historical_archive_complete": (
                report["all_historical_orders_complete"]
            ),
            "nonflat_restart_recovery_certified": False,
            "native_sl_trigger_origin_certified": False,
            "live_authorization": False, "mexc_orders_sent": 0,
        }
    assert prior is not None
    if not hmac.compare_digest(prior["evidence_hmac_sha256"], tag):
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_EXCHANGE_EVIDENCE_CHANGED")
    return {
        "status": "THIRD_CLOSED_PILOT_RESTART_REPLAY_VERIFIED_NONAUTHORIZING",
        "new_local_receipt_created": False,
        "receipt_matches_fresh_mexc_get": True,
        "restarted_process_replay_verified": True,
        "account_bookended_flat_verified": True,
        "source_historical_archive_complete": (
            report["all_historical_orders_complete"]
        ),
        "nonflat_restart_recovery_certified": False,
        "native_sl_trigger_origin_certified": False,
        "live_authorization": False, "mexc_orders_sent": 0,
    }


async def _main(action: str) -> None:
    from app.mexc_trade import MexcTradeClient
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("CLOSED_RECEIPT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        result = await execute(
            action, client=client, api_secret=secret,
            account_fingerprint=fingerprint,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        print("MEXC_SIGNED_GET_ONLY_NO_ORDERS=true")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Actual closed BTC pilot durable receipt / restart replay"
    )
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--seal", action="store_true")
    flags.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    try:
        with OneShotProcessLock():
            asyncio.run(_main("seal" if args.seal else "replay"))
    except BaseException as exc:
        reason = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("CLOSED_RECEIPT_HALTED=" + reason)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
