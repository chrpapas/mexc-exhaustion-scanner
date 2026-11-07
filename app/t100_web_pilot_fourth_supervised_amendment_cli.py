"""FOURTH ONE-SHOT REAL MEXC pilot: supervised amendment of exact attached BTC SL.

Narrow exception applies to this experiment ONLY: current public MEXC GET
responses omit stopLossType for an attached TP/SL. This module accepts
*absence*, never a stated LIMIT type, only with (1) all other original
native.strict checks true, (2) exact signed second unfinished-TP/SL GET
matching the stop and full quantity, (3) operator confirms the live MEXC UI
explicitly says Trigger Market SL, and (4) strict 1-contract 1x isolated,
account/identity/price checks. This is NOT machine proof of market SL and
NEVER enables unattended production or bypasses its stop-type gates.

--check     prior third market exit exact independently verified, account
            bookended flat, fresh one-contract preview, no fourth journal
--open      ONE real market short with embedded 50bp SL, journal before POST
--inspect   signed GET-only, TWO independent stop routes, original vs edited
--amend     explicit OPERATOR-supervised ONE real stop price-edit POST,
            journal before POST, no retry; readback exact same stop id
--status    GET-only prior pilot status/trigger for full close evidence
--close     one supervised 1-contract exact-owned market close if still open
--inventory full-account GET-only inventory

NEVER unattended. Protectiveness in exchange UI MUST be independently
confirmed by operator before the amendment. New journal != prior pilot.
No touching T100/225 main trader, BOXWAVE, manual positions or Render.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from decimal import Decimal
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_native_stop_amendment_cli as native
from app import t100_web_pilot_third_close_audit_cli as previous
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as inventory_run,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.mexc_trade import MexcTradeClient

D = Decimal
FOURTH_JOURNAL = pilot.BASE_DIR / "supervised_native_stop_amendment_004.json"
CONFIRM_PHRASE = "I SEE ONE BTC SHORT WITH A TRIGGER MARKET SL AND AUTHORIZE ONE EDIT"
STATUS_READY = "SUPERVISED_ATTACHED_STOP_EXACT_TWO_GET_READBACK_READY_NONAUTHORIZING"
STATUS_EDITED = "SUPERVISED_ONE_STOP_PRICE_EDIT_SAME_ID_GET_OBSERVED_NONAUTHORIZING"


def _read_journal(fp: str) -> dict[str, Any]:
    row = native._check_journal(fp)
    if row.get("experiment_kind") != "FOURTH_SUPERVISED_OMITTED_SL_TYPE":
        raise pilot.TriggerPilotHalted("FOURTH_JOURNAL_EXPERIMENT_PROFILE_INVALID")
    if row.get("stop_bps") != "50":
        raise pilot.TriggerPilotHalted("FOURTH_JOURNAL_STOP_PROFILE_INVALID")
    return row


async def _prior_close_and_flat(client: Any, fp: str) -> dict[str, Any]:
    result = await previous.audit_third_pilot(client, fp)
    if (
        result.get("status") !=
        "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
        or result.get("account_bookended_flat_verified") is not True
        or result.get("exact_close_order_filled") is not True
        or result.get("exact_close_trade_deals_filled") is not True
    ):
        raise pilot.TriggerPilotHalted("THIRD_PILOT_FULL_CLOSE_NOT_RECONFIRMED")
    latest = await inventory_run(client=InventoryGetOnlyClient(client))
    if not _is_flat(latest):
        raise pilot.TriggerPilotHalted("FOURTH_PILOT_ACCOUNT_NOT_FLAT")
    return {
        "third_pilot_real_close_confirmed": True,
        "current_web_subaccount_stably_flat": True,
        "fourth_journal_exists": FOURTH_JOURNAL.exists() or FOURTH_JOURNAL.is_symlink(),
    }


async def _observe_stop(
    client: Any, fp: str, *,
    allow_edited: bool = True,
) -> tuple[dict[str, Any], str | None, D | None]:
    """Two real signed GET routes + complete position/stop identity witness.

    No mutation and no full market-SL certification if MEXC omits type.
    """
    row = _read_journal(fp)
    safe = pilot._GetOnly(client)
    entry = await safe.get(
        f"/api/v1/private/order/external/{native.SYMBOL}/{row['external_oid']}"
    )
    if type(entry) is not dict or not (
        entry.get("externalOid") == row["external_oid"]
        and entry.get("symbol") == native.SYMBOL
        and type(entry.get("side")) is int and entry["side"] == 3
        and type(entry.get("state")) is int and entry["state"] == 3
        and pilot.positive(entry.get("dealVol")) == D(1)
        and pilot.positive(entry.get("vol")) == D(1)
        and pilot.id_string(entry.get("orderId")) is not None
        and pilot.id_string(entry.get("positionId")) is not None
        and row.get("entry_order_id") == pilot.id_string(entry["orderId"])
    ):
        raise pilot.TriggerPilotHalted("FOURTH_ENTRY_NOT_EXACT_OWNED_FILLED_SHORT")

    positions = await safe.get("/api/v1/private/position/open_positions")
    stops = await safe.get("/api/v1/private/stoporder/open_orders")
    counts = await safe.get("/api/v1/private/order/open_order_total_count")
    if (
        type(positions) is not list
        or type(stops) is not list
        or type(counts) is not dict
        or any(type(x) is not dict for x in positions + stops)
        or any(type(counts.get(k)) is not int or counts[k] < 0
               for k in ("sumCount", "stopOrderCount", "limitOrderCount",
                         "planOrderCount", "trackOrderCount"))
        or counts["sumCount"] != sum(
            counts[k] for k in (
                "stopOrderCount", "limitOrderCount",
                "planOrderCount", "trackOrderCount",
            )
        )
        or counts["stopOrderCount"] != len(stops)
    ):
        raise pilot.TriggerPilotHalted("FOURTH_ACCOUNT_INVENTORY_SHAPE_INVALID")
    out = {
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "position_count": len(positions),
        "stop_count": len(stops),
        "open_order_count": counts["sumCount"],
        "market_type_explicit_from_api": False,
        "market_type_missing_accepted_only_for_supervised_experiment": False,
        "operator_ui_market_stop_confirmation_required": True,
        "amend_post_attempted": row.get("amend_post_attempted", False),
        "amend_ack_verified": row.get("amend_ack_verified", False),
        "exchange_mutations_by_observe": 0,
        "live_authorization": False,
        "native_stop_market_type_certified": False,
        "native_amendment_live_certified": False,
    }
    if not positions and not stops and counts["sumCount"] == 0:
        out["status"] = "FOURTH_PILOT_ALREADY_FLAT_NO_AMEND"
        return out, None, None
    if (
        len(positions) != 1 or len(stops) != 1 or counts["sumCount"] != 1
        or counts["limitOrderCount"] != 0 or counts["planOrderCount"] != 0
        or counts["trackOrderCount"] != 0
    ):
        out["status"] = "FOURTH_UNRELATED_EXPOSURE_OR_ORDERS_BLOCK_AMEND"
        return out, None, None

    position, stop = positions[0], stops[0]
    position_id, order_id = (
        pilot.id_string(entry["positionId"]),
        pilot.id_string(entry["orderId"]),
    )
    checks = native._active_stop_redacted_checks(
        position=position, stop=stop,
        position_id=position_id, order_id=order_id,
    )
    extra_limit_price = pilot.positive(stop.get("stopLossOrderPrice"))
    if extra_limit_price is not None:
        checks["limit_sl_order_price_not_positive"] = False
    # Every original strict check is still mandatory except the ONE
    # documented API omission. An explicitly reported LIMIT is forbidden.
    nonmarket_failures = sorted(
        key for key, ok in checks.items()
        if key != "stop_is_market_type" and not ok
    )
    if nonmarket_failures:
        out["status"] = "FOURTH_EXACT_NATIVE_STOP_INVARIANT_FAILED"
        out["failed_strict_fields"] = nonmarket_failures
        return out, None, None
    if stop.get("stopLossType") not in (None, 0):
        out["status"] = "FOURTH_STOP_NOT_MARKET_OR_UNKNOWN_TYPE_UNSAFE"
        return out, None, None
    if type(stop.get("stopLossType")) is int and stop["stopLossType"] == 0:
        out["market_type_explicit_from_api"] = True
    else:
        out["market_type_missing_accepted_only_for_supervised_experiment"] = True

    second = await native._unfinished_stop_list_market_type_probe(
        client, exact_stop=stop,
        position_id=position_id, order_id=order_id,
    )
    out["second_official_get_exact_identity"] = second.get(
        "second_official_stop_get_exact_identity"
    ) is True
    out["second_official_get_stop_type"] = second.get(
        "second_official_stop_loss_type", "UNVERIFIED"
    )
    if (
        out["second_official_get_exact_identity"] is not True
        or out["second_official_get_stop_type"] not in ("MARKET", "FIELD_NOT_REPORTED")
    ):
        out["status"] = "FOURTH_SECOND_SIGNED_STOP_GET_INCONCLUSIVE"
        return out, None, None
    if out["second_official_get_stop_type"] == "MARKET":
        out["market_type_explicit_from_api"] = True

    stop_id = pilot.id_string(stop.get("id"))
    original = D(row["stop_price"])
    target = pilot.positive(row.get("amend_target_price"))
    price = pilot.positive(stop.get("stopLossPrice"))
    last, ask, _ = await native._current_ticker(client)
    if (
        stop_id is None or price is None or price <= ask
        or last <= 0
    ):
        out["status"] = "FOURTH_STOP_PRICE_INVALID_OR_ALREADY_TRIGGERED"
        return out, None, None

    if row.get("amend_post_attempted") is True:
        if (
            not allow_edited or row.get("amend_ack_verified") is not True
            or target is None or target >= original
            or stop_id != pilot.id_string(row.get("amend_stop_order_id"))
            or price != target
        ):
            out["status"] = "FOURTH_EDIT_UNKNOWN_OR_NOT_CONFIRMED_NO_RETRY"
            return out, None, None
        out["status"] = STATUS_EDITED
    elif price == original:
        out["status"] = STATUS_READY
    else:
        out["status"] = "FOURTH_ATTACHED_STOP_CHANGED_EXTERNALLY_NO_AMEND"
        return out, None, None
    out["same_exact_native_stop_order_id_observed"] = True
    out["one_contract_stop_quantity_observed"] = True
    out["stop_above_live_ask_observed"] = True
    out["exchange_continuous_protection_certified"] = False
    return out, stop_id, price


async def amend_once(client: Any, fp: str) -> dict[str, Any]:
    """One UI-confirmed real native stop price-edit, no retries."""
    journal = _read_journal(fp)
    if journal.get("amend_post_attempted") is True:
        raise pilot.TriggerPilotHalted("FOURTH_STOP_AMEND_ALREADY_ATTEMPTED_NO_RETRY")
    first, first_stop_id, _ = await _observe_stop(
        client, fp, allow_edited=False,
    )
    if first["status"] != STATUS_READY or first_stop_id is None:
        raise pilot.TriggerPilotHalted("FOURTH_AMEND_PREFLIGHT_FAILED")
    print("VERIFY_LIVE_MEXC_UI: exactly 1 BTC short, isolated 1x,")
    print("and its entire contract quantity protected by Trigger Market SL.")
    print("This supervised one-shot test proceeds despite absent native")
    print("stopLossType API field; production's strict gate remains BLOCKED.")
    pilot._confirm(CONFIRM_PHRASE)
    # Reconcile account again AFTER operator UI confirmation.
    second, second_stop_id, current_price = await _observe_stop(
        client, fp, allow_edited=False,
    )
    if (
        second["status"] != STATUS_READY
        or second_stop_id != first_stop_id
        or current_price != D(journal["stop_price"])
    ):
        raise pilot.TriggerPilotHalted("FOURTH_STOP_CHANGED_AFTER_UI_CONFIRMATION")
    meta = await native.PilotDryRunGetOnlyClient(client)._public_get(
        "/api/v1/contract/detail", {"symbol": native.SYMBOL},
    )
    tick = pilot.positive(native.one_contract_metadata(meta).get("priceUnit"))
    if tick is None:
        raise pilot.TriggerPilotHalted("FOURTH_CONTRACT_TICK_INVALID")
    reference = D(journal["reference_last_price"])
    target = native.candidate_amended_stop(reference=reference, tick=tick)
    last, ask, _ = await native._current_ticker(client)
    old = D(journal["stop_price"])
    if (
        target >= old or target <= ask
        or target / ask - D(1) < native.MIN_STOP_TO_MARKET_BPS / D(10000)
        or old / last - D(1) > D("0.02")
        or (target / reference - D(1)) * D(10000) > D("32")
    ):
        raise pilot.TriggerPilotHalted("FOURTH_TIGHTER_STOP_NOT_SAFELY_ABOVE_ASK")

    # Durable local mutation fence BEFORE exactly one real venue POST.
    journal.update({
        "amend_post_attempted": True,
        "amend_ack_verified": False,
        "amend_stop_order_id": first_stop_id,
        "amend_previous_price": str(old),
        "amend_target_price": str(target),
        "amend_post_status": "SUBMITTING_OR_UNKNOWN_NO_RETRY",
        "ui_operator_confirmed_market_sl_before_amend": True,
    })
    pilot._persist(journal)
    print("FOURTH_AMENDMENT_DURABLY_FENCED_ONE_POST_ATTEMPT=true")
    try:
        await client._private_post(native.AMEND_ENDPOINT, {
            "stopPlanOrderId": int(first_stop_id),
            "stopLossPrice": str(target),
            "lossTrend": 1,
            "stopLossReverse": 2,
        }, throttle=False)
        journal["amend_ack_verified"] = True
        journal["amend_post_status"] = "VENUE_ACK_READBACK_REQUIRED"
        pilot._persist(journal)
    except BaseException:
        print("FOURTH_AMEND_POST_OUTCOME_UNKNOWN_DO_NOT_RETRY=true")
        raise
    before, _, _ = await _observe_stop(client, fp)
    after, _, _ = await _observe_stop(client, fp)
    exact = (
        before["status"] == STATUS_EDITED
        and after["status"] == STATUS_EDITED
        and before.get("same_exact_native_stop_order_id_observed") is True
        and after.get("same_exact_native_stop_order_id_observed") is True
        and before.get("one_contract_stop_quantity_observed") is True
        and after.get("one_contract_stop_quantity_observed") is True
    )
    after.update({
        "supervised_one_edit_ack_observed": True,
        "one_exact_stop_id_two_independent_post_edit_readbacks": exact,
        "native_stop_market_type_certified": False,
        "native_amendment_live_certified": False,
        "exchange_continuous_protection_certified": False,
        "live_authorization": False,
    })
    return after


async def _main(action: str) -> dict[str, Any] | None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("FOURTH_PILOT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fp = hashlib.sha256(key.encode("utf-8")).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    original = (pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS)
    try:
        pilot.JOURNAL = FOURTH_JOURNAL
        pilot.STOP_BPS = native.INITIAL_STOP_BPS
        pilot.MAX_BPS = native.MAX_INITIAL_STOP_BPS
        if action == "check":
            prior = await _prior_close_and_flat(client, fp)
            if prior["fourth_journal_exists"]:
                raise pilot.TriggerPilotHalted("FOURTH_JOURNAL_EXISTS_NO_ENTRY_RETRY")
            return {**prior, "preview": await native.preview(client)}
        if action == "open":
            prior = await _prior_close_and_flat(client, fp)
            if prior["fourth_journal_exists"]:
                raise pilot.TriggerPilotHalted("FOURTH_JOURNAL_EXISTS_NO_ENTRY_RETRY")
            print("FOURTH_ONE_CONTRACT_REAL_TEST=OPERATOR_SUPERVISION_REQUIRED")
            await pilot._send(client, fp)
            return None
        if action == "inspect":
            result, _, _ = await _observe_stop(client, fp)
            return result
        if action == "amend":
            return await amend_once(client, fp)
        if action == "close":
            await pilot._close(client, fp)
            return None
        if action == "status":
            return await pilot._status(client, fp)
        if action == "inventory":
            book = await inventory_run(
                client=InventoryGetOnlyClient(client),
            )
            return {
                "account_stably_flat_verified": _is_flat(book),
                "position_count": book.get("after_positions"),
                "stop_count": book.get("after_current_tpsl"),
                "open_order_count": book.get("reported_open_order_counts", {}).get(
                    "sumCount",
                ),
                "read_only": True, "mexc_orders_sent": 0,
                "live_authorization": False,
            }
        raise pilot.TriggerPilotHalted("FOURTH_PILOT_ACTION_UNKNOWN")
    finally:
        pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS = original
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One supervised REAL 1x isolated BTC market-SL amendment"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ("check", "open", "inspect", "amend", "close", "status", "inventory"):
        group.add_argument("--" + action, action="store_true")
    args = parser.parse_args()
    action = next(
        action for action in ("check", "open", "inspect", "amend", "close", "status", "inventory")
        if getattr(args, action)
    )
    try:
        with OneShotProcessLock():
            report = asyncio.run(_main(action))
        if report is not None:
            print(json.dumps(report, indent=2, sort_keys=True))
        if action in ("check", "inspect", "status", "inventory"):
            print("SIGNED_GET_ONLY_NO_MEXC_ORDERS=true")
    except BaseException as exc:
        code = (
            str(exc) if isinstance(exc, pilot.TriggerPilotHalted)
            else type(exc).__name__
        )
        print("FOURTH_PILOT_HALTED=" + code)
        if action in ("open", "amend", "close"):
            print("DO_NOT_RETRY_UNCERTAIN_POST_MANAGE_EXCHANGE_UI=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
