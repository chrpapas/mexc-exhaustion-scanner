"""Explicit operator-only, ONE-SHOT REAL BTC_USDT short pilot on NEW T100 web subaccount.

Only --send and --close contain MEXC POST code, each one POST maximum and
each with its own durable pre-POST no-retry journal. --check and --status are
signed GET-only. No Render/DB/BOXWAVE access. An entry with an embedded stop
may still fill without usable protection. Do not leave the terminal/MEXC UI
unattended; use --close or the exchange UI if stop readback fails.

A successful pilot does NOT authorize autonomous or full-capital trading.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.t100_live_current_inventory_cli import read_pilot_keychain_credentials
from app.t100_web_pilot_dry_run_cli import PilotDryRunGetOnlyClient, check
from app.t100_web_pilot_entry_review import (
    assess_post_entry_snapshot, draft_entry_request,
)

JOURNAL = Path.home() / "Library" / "Application Support" / "T100WebPilot" / "one_contract_order.json"
SYMBOL = "BTC_USDT"
ORDER_CREATE = "/api/v1/private/order/create"
ENTRY_PHRASE = "PLACE ONE BTC SHORT WITH STOP"
CLOSE_PHRASE = "CLOSE MY ONE BTC SHORT"


class OneShotHalted(RuntimeError):
    pass


def _journal_dir() -> None:
    JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if JOURNAL.parent.is_symlink() or JOURNAL.is_symlink():
        raise OneShotHalted("UNSAFE_JOURNAL_PATH")
    os.chmod(JOURNAL.parent, 0o700)


def _durable_dir_sync() -> None:
    # Flush directory metadata as well as file content.
    fd = os.open(JOURNAL.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _first_journal(body: dict[str, Any]) -> None:
    _journal_dir()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(JOURNAL, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        _durable_dir_sync()
    except BaseException:
        # NEVER delete a possibly committed one-shot journal; if persistence
        # is uncertain, refuse another send, reconcile via --status or UI.
        raise


def _replace_journal(body: dict[str, Any]) -> None:
    _journal_dir()
    if not JOURNAL.is_file():
        raise OneShotHalted("PILOT_JOURNAL_NOT_FOUND")
    fd, temp = tempfile.mkstemp(dir=JOURNAL.parent, prefix=".pilot-journal-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, JOURNAL)
        _durable_dir_sync()
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _load_journal(fingerprint: str) -> dict[str, Any]:
    if not JOURNAL.is_file() or JOURNAL.is_symlink():
        raise OneShotHalted("PILOT_JOURNAL_MISSING_OR_UNSAFE")
    if JOURNAL.stat().st_mode & 0o077:
        raise OneShotHalted("PILOT_JOURNAL_PERMISSIONS_UNSAFE")
    data = json.loads(JOURNAL.read_text(encoding="utf-8"))
    if (
        type(data) is not dict or data.get("version") != 1
        or data.get("key_sha256") != fingerprint
        or data.get("symbol") != SYMBOL
        or not re.fullmatch(r"TPWEB[a-f0-9]{24}", str(data.get("external_oid", "")))
        or type(data.get("draft")) is not dict
        or data["draft"].get("externalOid") != data["external_oid"]
        or type(data.get("entry_post_attempted")) is not bool
    ):
        raise OneShotHalted("PILOT_JOURNAL_ACCOUNT_OR_SHAPE_MISMATCH")
    return data


def _allow_real_mode_env() -> None:
    # This one-shot CLI is separate from the general bot's live switch;
    # the bot stays kill-latched and in paper mode.
    if (
        os.environ.get("T100_EXECUTION_MODE") != "paper"
        or os.environ.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or os.environ.get("T100_LIVE_KILL_SWITCH") != "true"
    ):
        raise OneShotHalted("PAPER_KILL_ENVIRONMENT_REQUIRED")


def _positive_ascii_id(raw: Any) -> str:
    v = str(raw) if type(raw) in (int, str) else ""
    if re.fullmatch(r"[1-9][0-9]*", v) is None:
        raise OneShotHalted("EXCHANGE_ID_UNVERIFIED")
    return v


def _make_oid(prefix: str = "TPWEB") -> str:
    return prefix + secrets.token_hex(12)


def _confirmed(phrase: str) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise OneShotHalted("INTERACTIVE_TERMINAL_REQUIRED")
    print("This is a REAL MEXC FUTURES ORDER. A stop may fail to attach or execute.")
    print("Keep the MEXC website open. You MUST be able to close the short yourself.")
    if input("Type exactly " + repr(phrase) + ": ").strip() != phrase:
        raise OneShotHalted("OPERATOR_CONFIRMATION_DECLINED")


class PilotPostflightGetOnly:
    """Tightly limit private GET paths after an entry or close attempt."""

    __slots__ = ("__client",)

    def __init__(self, client: Any):
        self.__client = client

    async def get(self, path: str) -> Any:
        allowed = {
            "/api/v1/private/position/open_positions",
            "/api/v1/private/stoporder/open_orders",
            "/api/v1/private/order/open_order_total_count",
        }
        if (
            path not in allowed
            and re.fullmatch(
                r"/api/v1/private/order/external/BTC_USDT/TPWEB[a-f0-9]{24}",
                path,
            ) is None
        ):
            raise OneShotHalted("GET_NOT_ALLOWLISTED")
        return await self.__client._private_get(path)


async def _snapshot(client: Any, external_oid: str, draft: dict[str, Any]) -> dict[str, Any]:
    safe = PilotPostflightGetOnly(client)
    order = await safe.get(f"/api/v1/private/order/external/{SYMBOL}/{external_oid}")
    positions = await safe.get("/api/v1/private/position/open_positions")
    stops = await safe.get("/api/v1/private/stoporder/open_orders")
    counts = await safe.get("/api/v1/private/order/open_order_total_count")
    if (
        type(positions) is not list or type(stops) is not list
        or type(counts) is not dict
        or any(type(x) is not dict for x in positions + stops)
        or any(
            type(counts.get(k)) is not int or counts[k] < 0
            for k in ("sumCount", "stopOrderCount", "limitOrderCount",
                      "planOrderCount", "trackOrderCount")
        )
    ):
        raise OneShotHalted("POST_ENTRY_INVENTORY_SHAPE_INVALID")
    result = assess_post_entry_snapshot(
        order=order, positions=positions, stops=stops, draft=draft,
    )
    if counts["stopOrderCount"] != len(stops):
        result["status"] = "CURRENT_STOP_COUNT_MISMATCH"
        result["active_native_stop_fields_observed"] = False
    result["position_count"] = len(positions)
    result["stop_count"] = len(stops)
    result["pending_order_count"] = counts["sumCount"]
    result["manual_close_required_if_unprotected"] = (
        len(positions) > 0 and not result["active_native_stop_fields_observed"]
    )
    return result


async def _check(client: Any) -> dict[str, Any]:
    return await asyncio.wait_for(
        check(PilotDryRunGetOnlyClient(client)), timeout=55,
    )


async def _send(client: Any, fingerprint: str) -> None:
    _journal_dir()
    if JOURNAL.exists() or JOURNAL.is_symlink():
        raise OneShotHalted("PREVIOUS_PILOT_JOURNAL_EXISTS_NO_RETRY")
    preview = await _check(client)
    print("PREFLIGHT=" + str(preview["status"]))
    print("INDICATIVE_NOTIONAL_USDT=" + preview["indicative_notional_usdt"])
    print("CURRENT_BTC_LAST_USDT=" + preview["reference_last_price"])
    print("EMBEDDED_SL_USDT=" + preview["embedded_stop_loss"])
    print("ONE_CONTRACT_ISOLATED_1X=true")
    _confirmed(ENTRY_PHRASE)

    # Live conditions must still hold AFTER the user has confirmed.
    fresh = await _check(client)
    before = Decimal(preview["reference_last_price"])
    now = Decimal(fresh["reference_last_price"])
    if abs(now / before - 1) > Decimal("0.005"):
        raise OneShotHalted("PRICE_MOVED_OVER_HALF_PERCENT_RESTART_REVIEW")
    oid = _make_oid()
    draft = draft_entry_request(fresh, external_oid=oid)
    data = {
        "version": 1,
        "symbol": SYMBOL,
        "key_sha256": fingerprint,
        "external_oid": oid,
        "draft": draft,
        "entry_post_attempted": True,
        "entry_ack_verified": False,
        "entry_order_id": None,
        "close_post_attempted": False,
        "close_oid": None,
        "close_ack_verified": False,
        "status": "ENTRY_POST_OUTCOME_UNKNOWN_RECONCILE_ONLY",
    }
    # The ONLY entry POST is fenced BEFORE sending, including network
    # timeout or process crash; never retry an uncertain submission.
    _first_journal(data)
    print("ENTRY_JOURNALED_BEFORE_POST=true")
    print("ENTRY_POSTS_THIS_RUN=1")
    try:
        response = await client._private_post(ORDER_CREATE, draft, throttle=False)
        if type(response) is not dict:
            raise OneShotHalted("ENTRY_ACK_SHAPE_UNVERIFIED")
        order_id = _positive_ascii_id(response.get("orderId"))
        data["entry_ack_verified"] = True
        data["entry_order_id"] = order_id
        data["status"] = "ENTRY_ACKED_POSTFLIGHT_REQUIRED"
        _replace_journal(data)
        print("ENTRY_ACK_VERIFIED=true")
    except BaseException:
        print("ENTRY_OUTCOME_UNCERTAIN_NO_RETRY=true")
        print("CHECK_MEXC_UI_NOW=true")
        # Uncertain exchange outcome: never resubmit. Preserve journal.
        raise

    # An acknowledged order isn't a fill or protected position. Even if
    # readback fails, user must inspect and, if needed, close via MEXC UI.
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(0.7)
        try:
            report = await _snapshot(client, oid, draft)
        except Exception:
            continue
        print("POSTFLIGHT=" + json.dumps(report, sort_keys=True))
        if report.get("active_native_stop_fields_observed") is True:
            print("ACTIVE_STOP_FIELDS_OBSERVED=TRUE_STILL_NOT_EXECUTION_CERTIFIED")
            print("USE_--status_TO_RECHECK_AND_--close_TO_EXIT")
            return
    print("POSTFLIGHT_UNVERIFIED_OR_STOP_UNVERIFIED=true")
    print("URGENT_CHECK_MEXC_UI_AND_CLOSE_OPEN_SHORT_IF_UNPROTECTED=true")
    print("USE_--status_TO_RECONCILE_OR_--close_FOR_ONE_SHOT_CLOSE")


async def _status(client: Any, fingerprint: str) -> None:
    data = _load_journal(fingerprint)
    print("JOURNAL_ENTRY_POST_ATTEMPTED=" + str(data["entry_post_attempted"]).lower())
    print("JOURNAL_ENTRY_ACK_VERIFIED=" + str(data["entry_ack_verified"]).lower())
    print("JOURNAL_CLOSE_POST_ATTEMPTED=" + str(data["close_post_attempted"]).lower())
    report = await _snapshot(client, data["external_oid"], data["draft"])
    print("VENUE_POSTFLIGHT=" + json.dumps(report, sort_keys=True))
    print("NO_POST_REQUESTS_FROM_STATUS=true")


async def _close(client: Any, fingerprint: str) -> None:
    data = _load_journal(fingerprint)
    if data.get("close_post_attempted") is True:
        raise OneShotHalted("CLOSE_ALREADY_ATTEMPTED_NO_RETRY")
    if data.get("entry_post_attempted") is not True:
        raise OneShotHalted("NO_JOURNALED_ENTRY")
    safe = PilotPostflightGetOnly(client)
    order = await safe.get(f"/api/v1/private/order/external/{SYMBOL}/{data['external_oid']}")
    positions = await safe.get("/api/v1/private/position/open_positions")
    if type(order) is not dict or type(positions) is not list or len(positions) != 1:
        raise OneShotHalted("EXACT_OWNED_POSITION_NOT_OBSERVED")
    pos = positions[0]
    if type(pos) is not dict:
        raise OneShotHalted("POSITION_SHAPE_INVALID")
    order_position = _positive_ascii_id(order.get("positionId"))
    position_id = _positive_ascii_id(pos.get("positionId"))
    if (
        order.get("externalOid") != data["external_oid"]
        or order.get("symbol") != SYMBOL
        or order.get("side") != 3 or order.get("state") != 3
        or Decimal(str(order.get("dealVol"))) != Decimal(1)
        or order_position != position_id or pos.get("symbol") != SYMBOL
        or pos.get("positionType") != 2 or pos.get("openType") != 1
        or pos.get("leverage") != 1 or Decimal(str(pos.get("holdVol"))) != Decimal(1)
    ):
        raise OneShotHalted("OWNED_SHORT_IDENTITY_UNVERIFIED")
    print("EXACT_OWNED_ONE_CONTRACT_ISOLATED_SHORT_VERIFIED=true")
    _confirmed(CLOSE_PHRASE)
    # Recheck before closing, and refuse all intervening position changes.
    positions2 = await safe.get("/api/v1/private/position/open_positions")
    if positions2 != positions:
        raise OneShotHalted("POSITION_CHANGED_BEFORE_CLOSE")
    close_oid = _make_oid()
    body = {
        "symbol": SYMBOL, "price": "0", "vol": 1,
        "side": 2, "type": 5, "openType": 1,
        "positionId": int(position_id), "positionMode": 1,
        "externalOid": close_oid,
    }
    data["close_post_attempted"] = True
    data["close_oid"] = close_oid
    data["status"] = "CLOSE_POST_OUTCOME_UNKNOWN_RECONCILE_ONLY"
    _replace_journal(data)
    print("CLOSE_JOURNALED_BEFORE_POST=true")
    print("CLOSE_POSTS_THIS_RUN=1")
    response = await client._private_post(ORDER_CREATE, body, throttle=False)
    if type(response) is not dict:
        raise OneShotHalted("CLOSE_ACK_SHAPE_UNVERIFIED")
    _positive_ascii_id(response.get("orderId"))
    data["close_ack_verified"] = True
    data["status"] = "CLOSE_ACKED_FLAT_ACCOUNT_GET_UNVERIFIED"
    _replace_journal(data)
    print("CLOSE_ACK_VERIFIED=true")
    print("CLOSE_ACK_IS_NOT_FLAT_PROOF_CHECK_FUTURES_UI_AND_RUN_INVENTORY_AUDIT")


async def run(mode: str) -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise OneShotHalted("MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient("https://api.mexc.com", api_key=key, api_secret=secret)
    try:
        if mode == "check":
            if JOURNAL.exists():
                print("EXISTING_JOURNAL_PRESENT_NO_REPEAT_SEND=true")
            print(json.dumps(await _check(client), indent=2, sort_keys=True))
            print("GET_ONLY_NO_ORDERS=true")
        elif mode == "send":
            await _send(client, fingerprint)
        elif mode == "status":
            await _status(client, fingerprint)
        elif mode == "close":
            await _close(client, fingerprint)
        else:
            raise OneShotHalted("UNKNOWN_MODE")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot, risk-bounded T100 NEW web subaccount BTC_USDT pilot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="GET only, no order")
    group.add_argument("--send", action="store_true", help="REAL one-shot open short with embedded SL")
    group.add_argument("--status", action="store_true", help="GET only, reconcile existing pilot")
    group.add_argument("--close", action="store_true", help="REAL one-shot close of exact owned short")
    args = parser.parse_args()
    mode = next(
        k for k in ("check", "send", "status", "close") if getattr(args, k)
    )
    try:
        asyncio.run(run(mode))
    except BaseException as exc:
        # Never leak API keys, signed URLs, or venue response payloads.
        reason = str(exc) if isinstance(exc, OneShotHalted) else type(exc).__name__
        print("PILOT_HALTED=" + reason)
        if mode in ("send", "close"):
            print("DO_NOT_REPEAT_THIS_POST_WITHOUT_RECONCILIATION=true")
            print("CHECK_MEXC_FUTURES_UI_FOR_OPEN_UNPROTECTED_POSITION_NOW=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
