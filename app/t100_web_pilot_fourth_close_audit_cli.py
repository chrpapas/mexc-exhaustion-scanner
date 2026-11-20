"""Read-only forensic audit of the already closed FOURTH supervised MEXC pilot.

Exact journal + signed venue GETs only. No order creation, amendment, close,
journal mutation, main T100/225 authorization, BOXWAVE access or Render action.

A manually closed short with its stop canceled is NOT a successful SL trigger.
This script distinguishes: (a) exact filled manual market close, (b) exact
edited-stop terminal cancellation, and (c) incomplete history coverage.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from decimal import Decimal
from typing import Any

from app import t100_web_pilot_fourth_supervised_amendment_cli as fourth
from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app.t100_web_pilot_third_close_audit_cli import (
    HistoryGetOnly, _verify_exact_close,
)
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as run_inventory,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

D = Decimal


def _fourth_journal(fingerprint: str) -> dict[str, Any]:
    original = pilot.JOURNAL
    try:
        pilot.JOURNAL = fourth.FOURTH_JOURNAL
        return fourth._read_journal(fingerprint)
    finally:
        pilot.JOURNAL = original


async def audit_fourth_pilot(client: Any, fingerprint: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "FOURTH_CLOSE_AUDIT_NOT_CERTIFIED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_bookended_flat_verified": False,
        "fourth_entry_filled_verified": False,
        "fourth_amendment_ack_journaled": False,
        "exact_journaled_manual_close_identified": False,
        "exact_close_order_filled": False,
        "exact_close_trade_deals_filled": False,
        "close_market_order_type_verified": False,
        "close_contract_quantity_exact_one": False,
        "finished_stop_exact_identity_verified": False,
        "operator_ui_trigger_market_sl_confirmed_before_amend": False,
        "supervised_native_stop_full_one_contract_volume_observed": False,
        "finished_stop_price_matches_amended_target": False,
        "finished_stop_canceled_not_triggered": False,
        "finished_stop_error_code": "NOT_VERIFIED",
        "finished_stop_history_complete": False,
        "native_sl_trigger_and_market_close_verified": False,
        "exchange_continuous_protection_certified": False,
        "native_amendment_live_certified_for_production": False,
        "live_authorization": False,
        "entry_allowed": False,
        "read_only": True,
        "mexc_orders_sent": 0,
    }
    journal = _fourth_journal(fingerprint)
    if not (
        journal.get("amend_post_attempted") is True
        and journal.get("amend_ack_verified") is True
        and journal.get("close_post_attempted") is True
        and pilot.positive(journal.get("amend_target_price")) is not None
        and pilot.positive(journal.get("amend_target_price"))
        < pilot.positive(journal.get("stop_price"))
        and pilot.id_string(journal.get("amend_stop_order_id")) is not None
        and isinstance(journal.get("close_oid"), str)
        and pilot.OID_RE.fullmatch(journal["close_oid"])
    ):
        report["status"] = "FOURTH_JOURNAL_AMEND_OR_CLOSE_FENCE_UNVERIFIED"
        return report
    report["fourth_amendment_ack_journaled"] = True
    # The operator confirmed Trigger Market SL in the MEXC UI during the
    # supervised fourth pilot. This evidence is distinct from an API field
    # proving stopLossType=MARKET and from continuous protection through
    # every amendment/restart. Preserve the distinction in downstream reports.
    report["operator_ui_trigger_market_sl_confirmed_before_amend"] = (
        journal.get("ui_operator_confirmed_market_sl_before_amend") is True
    )

    before = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(before):
        report["status"] = "ACCOUNT_NOT_FLAT_AUDIT_HALTED"
        return report

    safe = HistoryGetOnly(client)
    entry = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{journal['external_oid']}"
    )
    eid = pilot.id_string(entry.get("orderId")) if type(entry) is dict else None
    pid = pilot.id_string(entry.get("positionId")) if type(entry) is dict else None
    if not (
        type(entry) is dict and eid is not None and pid is not None
        and entry.get("externalOid") == journal["external_oid"]
        and eid == journal.get("entry_order_id")
        and entry.get("symbol") == pilot.SYMBOL
        and type(entry.get("side")) is int and entry["side"] == 3
        and type(entry.get("state")) is int and entry["state"] == 3
        and type(entry.get("openType")) is int and entry["openType"] == 1
        and pilot.positive(entry.get("vol")) == D(1)
        and pilot.positive(entry.get("dealVol")) == D(1)
    ):
        report["status"] = "FOURTH_ENTRY_OR_OWNERSHIP_NOT_EXACT"
        return report
    report["fourth_entry_filled_verified"] = True

    # A journal-derived externalOid establishes exact manual-close identity.
    # No historical-list completeness assumption is needed for this GET.
    close = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{journal['close_oid']}"
    )
    cid = pilot.id_string(close.get("orderId")) if type(close) is dict else None
    if not (
        type(close) is dict and cid is not None and cid != eid
        and close.get("externalOid") == journal["close_oid"]
        and pilot.id_string(close.get("positionId")) == pid
        and close.get("symbol") == pilot.SYMBOL
        and type(close.get("side")) is int and close["side"] == 2
    ):
        report["status"] = "FOURTH_JOURNALED_CLOSE_ORDER_NOT_EXACT"
        return report
    report["exact_journaled_manual_close_identified"] = True
    verified = await _verify_exact_close(
        safe, close_order_id=cid, entry_position_id=pid, journal=journal,
    )
    report["exact_close_order_filled"] = (
        verified.get("exact_close_order_filled") is True
    )
    report["exact_close_trade_deals_filled"] = (
        verified.get("exact_close_trade_deals_filled") is True
    )
    report["close_market_order_type_verified"] = (
        verified.get("close_market_order_type_verified") is True
    )
    report["close_contract_quantity_exact_one"] = (
        verified.get("closed_contract_quantity_exact_one") is True
    )
    report["close_order_api_error"] = verified.get("close_api_error")
    report["close_deal_count"] = verified.get("close_deal_count", 0)

    # The third-pilot historical verifier is reused only for its exact
    # per-order fill checks. Its origin label is intentionally NOT copied.
    try:
        finished, pages = await pilot._history(
            pilot._GetOnly(client), eid, pid,
        )
        report["finished_stop_history_pages_read"] = pages
    except pilot.TriggerPilotHalted:
        finished = []
        report["finished_stop_history_status"] = "HISTORY_GET_INCONCLUSIVE"
    if len(finished) == 1:
        stop = finished[0]
        report["finished_stop_exact_identity_verified"] = (
            pilot.id_string(stop.get("id"))
            == pilot.id_string(journal.get("amend_stop_order_id"))
            and pilot.id_string(stop.get("orderId")) == eid
            and pilot.id_string(stop.get("positionId")) == pid
            and stop.get("symbol") == pilot.SYMBOL
            and stop.get("positionType") == 2
            and stop.get("profitLossVolType") == "SEPARATE"
            and pilot.positive(stop.get("stopLossVol")) == D(1)
        )
        report["supervised_native_stop_full_one_contract_volume_observed"] = (
            report["finished_stop_exact_identity_verified"]
            and report["fourth_entry_filled_verified"]
        )
        report["finished_stop_price_matches_amended_target"] = (
            pilot.positive(stop.get("stopLossPrice"))
            == pilot.positive(journal.get("amend_target_price"))
        )
        report["finished_stop_canceled_not_triggered"] = (
            type(stop.get("state")) is int and stop["state"] == 2
            and type(stop.get("triggerSide")) is int
            and stop["triggerSide"] == 0
            and type(stop.get("isFinished")) is int
            and stop["isFinished"] == 1
            and pilot.id_string(stop.get("placeOrderId")) is None
        )
        err = stop.get("errorCode")
        report["finished_stop_error_code"] = (
            "ZERO" if type(err) is int and err == 0
            else "NONZERO_REPORTED" if type(err) is int and err != 0
            else "UNREPORTED_OR_INVALID"
        )
    elif len(finished) > 1:
        report["finished_stop_history_status"] = "MULTIPLE_LINKED_ROWS"
    else:
        report.setdefault("finished_stop_history_status", "NOT_FOUND")

    after = await run_inventory(client=InventoryGetOnlyClient(client))
    report["account_bookended_flat_verified"] = (
        _is_flat(before) and _is_flat(after)
    )
    market_close = all((
        report["account_bookended_flat_verified"],
        report["exact_journaled_manual_close_identified"],
        report["exact_close_order_filled"],
        report["exact_close_trade_deals_filled"],
        report["close_market_order_type_verified"],
        report["close_contract_quantity_exact_one"],
    ))
    report["manual_market_close_exactly_verified"] = market_close
    if not report["account_bookended_flat_verified"]:
        report["status"] = "ACCOUNT_CHANGED_DURING_AUDIT"
    elif not market_close:
        report["status"] = "MANUAL_CLOSE_FILL_NOT_FULLY_VERIFIED"
    elif all((
        report["finished_stop_exact_identity_verified"],
        report["finished_stop_price_matches_amended_target"],
        report["finished_stop_canceled_not_triggered"],
    )):
        report["status"] = (
            "FOURTH_EXACT_MANUAL_MARKET_CLOSE_AND_EDITED_STOP_CANCELED_VERIFIED"
            "_NONAUTHORIZING"
        )
    else:
        report["status"] = "MANUAL_MARKET_CLOSE_VERIFIED_STOP_HISTORY_INCONCLUSIVE"
    return report


async def _main() -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("FOURTH_CLOSE_AUDIT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        report = await audit_fourth_pilot(client, fingerprint)
        print(json.dumps(report, indent=2, sort_keys=True))
        print("SIGNED_GET_ONLY_NO_ORDERS_OR_JOURNAL_MUTATIONS=true")
    finally:
        await client.close()


def main() -> None:
    try:
        with OneShotProcessLock():
            asyncio.run(_main())
    except BaseException as exc:
        code = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("FOURTH_CLOSE_AUDIT_HALTED=" + code)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
