"""Read-only exact fifth pilot native exit forensics for MEXC TP/SL conflict.

Only signed venue GETs + existing immutable fifth journal. This audit can
corroborate a venue-generated full market exit linked to the attached stop
record despite a historical triggerSide=TP. It NEVER silently relabels TP as
SL, certifies uninterrupted protection, enables trading, or sends an order.

The user-observed status FULL_MARKET_CLOSE_CONFIRMED_BUT_TRIGGER_SIDE_TP_CONFLICT
is a data provenance conflict, not proof of an SL trigger or its failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from decimal import Decimal
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_fifth_native_stop_trigger_cli as fifth
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


def _read_fifth_journal(fingerprint: str) -> dict[str, Any]:
    original = pilot.JOURNAL
    try:
        pilot.JOURNAL = fifth.FIFTH_JOURNAL
        return pilot._load(fingerprint)
    finally:
        pilot.JOURNAL = original


async def audit_fifth(client: Any, fingerprint: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "FIFTH_NATIVE_EXIT_EVIDENCE_INCOMPLETE",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_bookended_flat_verified": False,
        "entry_exact_filled": False,
        "manual_close_attempted_in_fifth_journal": "UNVERIFIED",
        "one_attached_finished_stop_row": False,
        "stop_price_matches_fifth_journal": False,
        "stop_order_terminal_executed": False,
        "historical_trigger_side": "UNVERIFIED",
        "no_take_profit_price_observed": False,
        "exchange_generated_close_id_present": False,
        "generated_close_exact_full_market_order": False,
        "generated_close_full_quantity_deals_verified": False,
        "generated_close_deal_count": 0,
        "historical_stop_error_code": "UNVERIFIED",
        "historical_stop_loss_type": "UNVERIFIED",
        "finished_stop_history_complete": False,
        "native_attached_exit_execution_correlated": False,
        "native_sl_trigger_certified": False,
        "exchange_continuous_protection_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "read_only": True,
        "mexc_orders_sent": 0,
    }
    journal = _read_fifth_journal(fingerprint)
    out["manual_close_attempted_in_fifth_journal"] = journal["close_post_attempted"]
    if (
        journal.get("stop_bps") != "5"
        or journal.get("entry_post_attempted") is not True
        or journal.get("entry_ack_verified") is not True
        or pilot.positive(journal.get("stop_price")) is None
        or pilot.id_string(journal.get("entry_order_id")) is None
    ):
        out["status"] = "FIFTH_JOURNAL_NOT_EXACT"
        return out

    before = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(before):
        out["status"] = "FIFTH_ACCOUNT_NOT_FLAT_AUDIT_HALTED"
        return out

    safe = HistoryGetOnly(client)
    entry = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{journal['external_oid']}"
    )
    eid = pilot.id_string(entry.get("orderId")) if type(entry) is dict else None
    pid = pilot.id_string(entry.get("positionId")) if type(entry) is dict else None
    entry_ok = (
        type(entry) is dict and eid is not None and pid is not None
        and eid == journal["entry_order_id"]
        and entry.get("externalOid") == journal["external_oid"]
        and entry.get("symbol") == pilot.SYMBOL
        and type(entry.get("side")) is int and entry["side"] == 3
        and type(entry.get("state")) is int and entry["state"] == 3
        and type(entry.get("openType")) is int and entry["openType"] == 1
        and pilot.positive(entry.get("vol")) == Decimal(1)
        and pilot.positive(entry.get("dealVol")) == Decimal(1)
    )
    out["entry_exact_filled"] = entry_ok
    if not entry_ok:
        out["status"] = "FIFTH_ENTRY_NOT_EXACT"
        return out

    finished, pages = await pilot._history(pilot._GetOnly(client), eid, pid)
    out["finished_stop_pages_read"] = pages
    if len(finished) != 1:
        out["status"] = (
            "FIFTH_FINISHED_STOP_AMBIGUOUS" if len(finished) > 1
            else "FIFTH_FINISHED_STOP_NOT_FOUND"
        )
        return out
    stop = finished[0]
    stop_identified = (
        pilot.id_string(stop.get("id")) is not None
        and pilot.id_string(stop.get("orderId")) == eid
        and pilot.id_string(stop.get("positionId")) == pid
        and stop.get("symbol") == pilot.SYMBOL
        and type(stop.get("positionType")) is int
        and stop["positionType"] == 2
        and stop.get("profitLossVolType") == "SEPARATE"
        and pilot.positive(stop.get("stopLossVol")) == Decimal(1)
    )
    out["one_attached_finished_stop_row"] = stop_identified
    out["stop_price_matches_fifth_journal"] = (
        pilot.positive(stop.get("stopLossPrice"))
        == pilot.positive(journal["stop_price"])
    )
    out["stop_order_terminal_executed"] = (
        type(stop.get("state")) is int and stop["state"] == 3
        and type(stop.get("isFinished")) is int
        and stop["isFinished"] == 1
    )
    side = stop.get("triggerSide")
    out["historical_trigger_side"] = (
        "SL" if type(side) is int and side == 2 else
        "TP" if type(side) is int and side == 1 else
        "NOT_TRIGGERED" if type(side) is int and side == 0 else "UNVERIFIED"
    )
    out["no_take_profit_price_observed"] = (
        pilot.positive(stop.get("takeProfitPrice")) is None
    )
    out["exchange_generated_close_id_present"] = (
        pilot.id_string(stop.get("placeOrderId")) is not None
    )
    err = stop.get("errorCode")
    out["historical_stop_error_code"] = (
        "ZERO" if type(err) is int and err == 0 else
        "NONZERO" if type(err) is int else "NOT_REPORTED"
    )
    typ = stop.get("stopLossType")
    out["historical_stop_loss_type"] = (
        "MARKET" if type(typ) is int and typ == 0 else
        "LIMIT" if type(typ) is int and typ == 1 else
        "MISSING_OR_UNKNOWN"
    )

    close_id = pilot.id_string(stop.get("placeOrderId"))
    if (
        not stop_identified
        or not out["stop_price_matches_fifth_journal"]
        or not out["stop_order_terminal_executed"]
        or close_id is None
        or out["historical_stop_loss_type"] == "LIMIT"
        or out["historical_trigger_side"] not in ("SL", "TP")
    ):
        out["status"] = "FIFTH_STOP_OR_EXIT_LINK_NOT_VERIFIED"
        return out
    # Independent GET of the generated close AND all its actual deals.
    verify = await _verify_exact_close(
        safe, close_order_id=close_id, entry_position_id=pid,
        journal=journal,
    )
    out["generated_close_exact_full_market_order"] = (
        verify.get("exact_close_order_filled") is True
        and verify.get("close_market_order_type_verified") is True
        and verify.get("closed_contract_quantity_exact_one") is True
        and verify.get("close_api_error") in ("ZERO", "NOT_REPORTED")
    )
    out["generated_close_full_quantity_deals_verified"] = (
        verify.get("exact_close_trade_deals_filled") is True
    )
    out["generated_close_deal_count"] = verify.get("close_deal_count", 0)
    after = await run_inventory(client=InventoryGetOnlyClient(client))
    out["account_bookended_flat_verified"] = (
        _is_flat(before) and _is_flat(after)
    )
    correlated = all((
        out["account_bookended_flat_verified"],
        out["one_attached_finished_stop_row"],
        out["stop_price_matches_fifth_journal"],
        out["stop_order_terminal_executed"],
        out["exchange_generated_close_id_present"],
        out["generated_close_exact_full_market_order"],
        out["generated_close_full_quantity_deals_verified"],
        journal["close_post_attempted"] is False,
    ))
    out["native_attached_exit_execution_correlated"] = correlated
    # An exact generated market close and absence of a TP price is strong
    # evidence of stop-linked execution, but cannot rewrite triggerSide=TP.
    out["native_sl_trigger_certified"] = bool(
        correlated
        and out["historical_trigger_side"] == "SL"
        and out["no_take_profit_price_observed"]
        and out["historical_stop_error_code"] == "ZERO"
    )
    if not correlated:
        out["status"] = "FIFTH_GENERATED_EXIT_OR_FLATNESS_UNVERIFIED"
    elif out["historical_trigger_side"] == "TP":
        out["status"] = (
            "FIFTH_NATIVE_ATTACHED_EXIT_EXACT_MARKET_FILL_VERIFIED"
            "_TP_CLASSIFICATION_CONFLICT_NONAUTHORIZING"
        )
    elif out["native_sl_trigger_certified"]:
        out["status"] = "FIFTH_NATIVE_SL_TRIGGER_EXACT_FILL_VERIFIED_NONAUTHORIZING"
    else:
        out["status"] = "FIFTH_SL_FIELD_OR_ERROR_UNVERIFIED_NONAUTHORIZING"
    return out


async def _main() -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("FIFTH_NATIVE_EXIT_AUDIT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fp = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        result = await audit_fifth(client, fp)
        print(json.dumps(result, indent=2, sort_keys=True))
        print("SIGNED_GET_ONLY_NO_MEXC_ORDERS_OR_JOURNAL_MUTATIONS=true")
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
        print("FIFTH_NATIVE_EXIT_AUDIT_HALTED=" + code)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
