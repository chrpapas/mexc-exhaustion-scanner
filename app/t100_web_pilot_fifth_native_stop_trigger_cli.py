"""FIFTH isolated one-shot MEXC pilot: observe an actual native stop trigger.

Scope: dedicated T100 WEB PILOT subaccount ONLY. The fourth pilot's exact manual
market close and edited-stop cancellation MUST be reverified from venue signed
GETs before ANY new entry attempt. A fresh immutable fifth journal fences a
maximum of one possible BTC 1-contract isolated 1x entry POST and one optional
supervised exact-owned manual close POST; no automatic retry.

--check     GET-only fourth audit + fresh account-flat and 5bp preview
--open      explicit operator-supervised real ONE-SHOT BTC short with embedded SL
--status    GET-only exact fifth entry, stop trigger, fill and flat reconciliation
--watch     bounded GET-only watch (up to 4 minutes), never performs a close
--close     explicit operator-supervised exact-owned short close, ONE POST only
--inventory GET-only whole pilot-account inventory

MEXC may fail to attach, trigger, or execute the SL; do not run unattended.
Inconsistent stop triggerSide is recorded, never silently rewritten. A flat
position or an order ACK cannot prove native trigger execution. An observed
native trigger/fill alone does NOT authorize unattended production trading.

No changes to T100/225 main trader, Render, BOXWAVE, their credentials,
manual positions, main production flags, or independent journals.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_fourth_close_audit_cli as fourth_audit
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as inventory_run,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

FIFTH_JOURNAL = pilot.BASE_DIR / "native_stop_trigger_005.json"
PREVIOUS_STATUS = (
    "FOURTH_EXACT_MANUAL_MARKET_CLOSE_AND_EDITED_STOP_CANCELED_VERIFIED"
    "_NONAUTHORIZING"
)


async def _prior_and_preview(client: Any, fp: str) -> dict[str, Any]:
    """Fail closed unless the exact previous pilot and current account reconcile."""
    if FIFTH_JOURNAL.exists() or FIFTH_JOURNAL.is_symlink():
        raise pilot.TriggerPilotHalted("FIFTH_PILOT_JOURNAL_EXISTS_NO_NEW_ENTRY")

    previous = await fourth_audit.audit_fourth_pilot(client, fp)
    if not (
        previous.get("status") == PREVIOUS_STATUS
        and previous.get("fourth_entry_filled_verified") is True
        and previous.get("fourth_amendment_ack_journaled") is True
        and previous.get("manual_market_close_exactly_verified") is True
        and previous.get("finished_stop_exact_identity_verified") is True
        and previous.get("finished_stop_price_matches_amended_target") is True
        and previous.get("finished_stop_canceled_not_triggered") is True
        and previous.get("account_bookended_flat_verified") is True
        and previous.get("live_authorization") is False
        and previous.get("mexc_orders_sent") == 0
    ):
        raise pilot.TriggerPilotHalted("FOURTH_EXACT_EXIT_NOT_RECONFIRMED")

    book = await inventory_run(client=InventoryGetOnlyClient(client))
    if not _is_flat(book):
        raise pilot.TriggerPilotHalted("FIFTH_PILOT_ACCOUNT_NOT_STABLY_FLAT")

    preview = await pilot._preview(client)
    if not (
        preview.get("status") == "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED"
        and preview.get("stop_bps") == "5"
        and preview.get("contracts") == 1
        and preview.get("margin") == "ISOLATED"
        and preview.get("leverage") == "1x"
        and preview.get("max_notional_usdt") == "12.50"
        and preview.get("live_authorization") is False
        and preview.get("mexc_orders_sent") == 0
    ):
        raise pilot.TriggerPilotHalted("FIFTH_PILOT_TIGHT_STOP_PREVIEW_INVALID")
    return {
        "status": "FIFTH_NATIVE_TRIGGER_ONE_SHOT_GET_ONLY_PREFLIGHT_READY",
        "previous_fourth_exact_exit_reconfirmed": True,
        "current_subaccount_stably_flat": True,
        "fifth_journal_absent": True,
        "preview": preview,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": 0,
    }


async def _main(action: str) -> dict[str, Any] | None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("FIFTH_NATIVE_TRIGGER_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    old_journal, old_stop, old_max = (
        pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS,
    )
    try:
        pilot.JOURNAL = FIFTH_JOURNAL
        pilot.STOP_BPS = pilot.D(5)
        pilot.MAX_BPS = pilot.D(7)
        if action == "check":
            return await _prior_and_preview(client, fingerprint)
        if action == "open":
            await _prior_and_preview(client, fingerprint)
            print("FIFTH_REAL_PILOT_OPERATOR_SUPERVISION_REQUIRED=true")
            print("ONE_CONTRACT_BTC_ISOLATED_1X_EMBEDDED_5BP_SL=true")
            print("WARNING_SL_ATTACHMENT_TRIGGER_AND_FILL_NOT_GUARANTEED=true")
            await pilot._send(client, fingerprint)
            return None
        if action == "status":
            result = await pilot._status(client, fingerprint)
            result["fifth_pilot_scope"] = "NATIVE_TRIGGER_EXACT_ONCE"
            result["live_authorization"] = False
            result["entry_allowed"] = False
            return result
        if action == "watch":
            await pilot._watch(client, fingerprint)
            return None
        if action == "close":
            print("ONLY_IF_EXACT_FIFTH_SHORT_STILL_OPEN=true")
            await pilot._close(client, fingerprint)
            return None
        if action == "inventory":
            book = await inventory_run(client=InventoryGetOnlyClient(client))
            return {
                "status": "FIFTH_PILOT_ACCOUNT_GET_ONLY_INVENTORY",
                "account_stably_flat_verified": _is_flat(book),
                "position_count": book.get("after_positions"),
                "stop_count": book.get("after_current_tpsl"),
                "open_order_count": book.get("reported_open_order_counts", {}).get(
                    "sumCount"
                ),
                "read_only": True,
                "live_authorization": False,
                "entry_allowed": False,
                "mexc_orders_sent": 0,
            }
        raise pilot.TriggerPilotHalted("FIFTH_PILOT_ACTION_UNKNOWN")
    finally:
        pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS = (
            old_journal, old_stop, old_max,
        )
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supervised fifth real BTC 1-contract native stop trigger pilot"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ("check", "open", "status", "watch", "close", "inventory"):
        group.add_argument("--" + action, action="store_true")
    args = parser.parse_args()
    action = next(
        action for action in ("check", "open", "status", "watch", "close", "inventory")
        if getattr(args, action)
    )
    try:
        with OneShotProcessLock():
            report = asyncio.run(_main(action))
        if report is not None:
            print(json.dumps(report, indent=2, sort_keys=True))
        if action in ("check", "status", "watch", "inventory"):
            print("SIGNED_GET_ONLY_NO_MEXC_ORDERS=true")
    except BaseException as exc:
        code = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("FIFTH_NATIVE_TRIGGER_PILOT_HALTED=" + code)
        if action in ("open", "close"):
            print("DO_NOT_RETRY_UNCERTAIN_POST_CHECK_EXCHANGE_UI=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
