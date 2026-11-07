"""Second isolated real 5bp T100 WEB pilot, only after prior journal close verified.

Never deletes or replaces prior one-shot journal. This is a separate one-time
operator CLI. --check/--send require durable proof that the immediately prior
tight-stop pilot closed fully, and a new GET-only flat full-account witness.
The close is performed SEPARATELY by the original pilot CLI; not by this
module. --send uses the original hardened one-shot implementation, but a
distinct exclusive journal. --status, --watch are GET-only. --close has the
same exact-owned confirmation/no-retry protections as the original. No
Render/BOXWAVE/DB access or automatic production authorization.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as inventory_run,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

PREVIOUS_JOURNAL = pilot.JOURNAL
SECOND_JOURNAL = pilot.BASE_DIR / "tight_stop_trigger_002.json"
EXPECTED_STOP_BPS = "5"
OID = re.compile(r"TPWEB[a-f0-9]{24}\\Z")


def _valid_oid(raw: Any) -> bool:
    return type(raw) is str and OID.fullmatch(raw) is not None


async def require_previous_close_verified(
    client: Any, fingerprint: str,
) -> dict[str, Any]:
    """Fail closed: previous own journal + exact 1-contract close + flat GET.

    A local close ACK alone is not fill proof. Never infer flat from a
    canceled historical stop, a missing position response or operator text.
    """
    if pilot.JOURNAL != PREVIOUS_JOURNAL:
        raise pilot.TriggerPilotHalted("SECOND_PILOT_JOURNAL_SCOPE_MISMATCH")
    previous = pilot._load(fingerprint)
    if (
        previous.get("entry_post_attempted") is not True
        or previous.get("entry_ack_verified") is not True
        or previous.get("close_post_attempted") is not True
        or previous.get("close_ack_verified") is not True
        or previous.get("stop_bps") not in ("5", "15")
        or not _valid_oid(previous.get("external_oid"))
        or not _valid_oid(previous.get("close_oid"))
        or previous["external_oid"] == previous["close_oid"]
    ):
        raise pilot.TriggerPilotHalted("PREVIOUS_PILOT_CLOSE_ACK_NOT_VERIFIED")

    safe = pilot._GetOnly(client)
    entry = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{previous['external_oid']}"
    )
    close = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{previous['close_oid']}"
    )
    if type(entry) is not dict or type(close) is not dict:
        raise pilot.TriggerPilotHalted("PREVIOUS_PILOT_ORIGINS_UNVERIFIED")

    entry_id = pilot.id_string(entry.get("orderId"))
    position_id = pilot.id_string(entry.get("positionId"))
    close_id = pilot.id_string(close.get("orderId"))
    if (
        entry_id is None or close_id is None or position_id is None
        or entry_id == close_id
        or entry_id != previous.get("entry_order_id")
        or entry.get("externalOid") != previous["external_oid"]
        or close.get("externalOid") != previous["close_oid"]
        or entry.get("symbol") != pilot.SYMBOL
        or close.get("symbol") != pilot.SYMBOL
        or entry.get("side") != 3
        or close.get("side") != 2
        or entry.get("openType") != 1
        or close.get("openType") != 1
        or entry.get("state") != 3
        or close.get("state") != 3
        or pilot.id_string(close.get("positionId")) != position_id
        or pilot.positive(entry.get("dealVol")) != pilot.D(1)
        or pilot.positive(entry.get("vol")) != pilot.D(1)
        or pilot.positive(close.get("dealVol")) != pilot.D(1)
        or pilot.positive(close.get("vol")) != pilot.D(1)
    ):
        raise pilot.TriggerPilotHalted("PREVIOUS_PILOT_FULL_CLOSE_NOT_VERIFIED")
    inventory = await inventory_run(client=InventoryGetOnlyClient(client))
    if not _is_flat(inventory):
        raise pilot.TriggerPilotHalted("PREVIOUS_PILOT_ACCOUNT_NOT_STABLY_FLAT")
    return {
        "previous_pilot_entry_and_close_fills_verified": True,
        "previous_pilot_account_flat_verified": True,
        "previous_stop_bps": previous["stop_bps"],
        "second_pilot_journal_exists": SECOND_JOURNAL.exists(),
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": 0,
    }


async def run(action: str) -> dict[str, Any] | None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("SECOND_PILOT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        if action in ("check", "send"):
            prior = await require_previous_close_verified(client, fingerprint)
            if prior["second_pilot_journal_exists"]:
                raise pilot.TriggerPilotHalted(
                    "SECOND_PILOT_JOURNAL_EXISTS_NO_NEW_ENTRY"
                )
        else:
            prior = None

        # Only the new session changes the in-process journal selector;
        # no bytes or disk files belonging to session 001 are modified.
        pilot.JOURNAL = SECOND_JOURNAL
        if action == "check":
            preview = await pilot._preview(client)
            return dict(prior, second_pilot_preview=preview)
        if action == "send":
            await pilot._send(client, fingerprint)
            return None
        if action == "status":
            return await pilot._status(client, fingerprint)
        if action == "watch":
            await pilot._watch(client, fingerprint)
            return None
        if action == "close":
            await pilot._close(client, fingerprint)
            return None
        raise pilot.TriggerPilotHalted("SECOND_PILOT_ACTION_INVALID")
    finally:
        pilot.JOURNAL = PREVIOUS_JOURNAL
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Second one-shot T100 5bp real SL test, after first closes",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "send", "status", "watch", "close"):
        mode.add_argument("--" + flag, action="store_true")
    args = parser.parse_args()
    action = next(
        name for name in ("check", "send", "status", "watch", "close")
        if getattr(args, name)
    )
    try:
        with OneShotProcessLock():
            result = asyncio.run(run(action))
        if result is not None:
            print(json.dumps(result, sort_keys=True, indent=2))
        if action in ("check", "status"):
            print("GET_ONLY_NO_ORDERS=true")
    except BaseException as exc:
        # Redact external API/HTTP exceptions, which might include keys.
        if isinstance(
            exc, (pilot.TriggerPilotHalted, pilot.PilotDryRunHalted),
        ):
            reason = str(exc)
        else:
            reason = type(exc).__name__
        print("SECOND_PILOT_HALTED=" + reason)
        if action in ("send", "close"):
            print("DO_NOT_RETRY_UNCERTAIN_POST_CHECK_MEXC_UI=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
