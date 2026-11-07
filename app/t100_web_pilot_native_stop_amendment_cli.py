"""REAL MEXC native TP/SL amendment pilot — third isolated, one-shot T100 experiment.

Purpose: verify official `POST /api/v1/private/stoporder/change_plan_price`
actually tightens the EXISTING attached native SL for a SINGLE, 1x isolated,
one-contract short, with no cancel/replace or duplicate stop order.

PREVIOUS dedicated 5bp pilot MUST have an independently GET-proven full
market close and stable flat subaccount before this tool permits a new entry.

Actions:
  --check     signed GET only: previous pilot complete + account flat +
              fresh eligible one-contract preview, initial stop 50bp
  --open      explicit operator confirmation; one new market short with a
              50bp native attached stop; independent exclusive journal
  --inspect   signed GET only: current position, stop, and amendment readback
  --amend     explicit operator confirmation: exactly ONE venue POST to
              tighten the same native SL to 30bp above entry reference.
              Journal BEFORE POST; NEVER retry ambiguous outcomes.
  --close     operator-confirmed exact-owned short market close if still open
  --inventory signed GET-only account-wide inventory

All other T100 live/prod controls remain KILL-LATCHED; BOXWAVE/Render/manual
positions untouched. This is an endpoint-certification experiment, NOT a
test of 14% frozen activation or continuous 0.5pp trailing strategy.
Do NOT run unattended. Native SL and close requests can fail or slip.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app.t100_web_pilot_second_tight_stop_cli import SECOND_JOURNAL
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient,
    _run as inventory_run,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock,
    _allow_real_mode_env,
)
from app.t100_web_pilot_dry_run_cli import (
    PilotDryRunHalted,
    PilotDryRunGetOnlyClient,
    one_contract_metadata,
)
from app.mexc_trade import MexcTradeClient

D = Decimal
SYMBOL = "BTC_USDT"
THIRD_JOURNAL = pilot.BASE_DIR / "native_stop_amendment_001.json"
ORIGINAL_JOURNAL = pilot.JOURNAL
INITIAL_STOP_BPS = D(50)
AMENDED_STOP_BPS = D(30)
MAX_INITIAL_STOP_BPS = D(52)
MIN_STOP_TO_MARKET_BPS = D(12)
AMEND_ENDPOINT = "/api/v1/private/stoporder/change_plan_price"


async def prior_pilot_market_close_and_flat(client: Any, fingerprint: str) -> dict[str, Any]:
    """Exact, signed GET proof of prior 002 trade exit; no SL-origin claim."""
    original = pilot.JOURNAL
    try:
        pilot.JOURNAL = SECOND_JOURNAL
        evidence = await pilot._status(client, fingerprint)
    finally:
        pilot.JOURNAL = original
    if (
        evidence.get("status")
        not in (
            "FULL_MARKET_CLOSE_CONFIRMED_BUT_TRIGGER_SIDE_TP_CONFLICT",
            "EXACT_SL_TRIGGER_AND_MARKET_CLOSE_FILL_VERIFIED_NONAUTHORIZING",
        )
        or evidence.get("entry_filled") is not True
        or evidence.get("account_stably_flat_verified") is not True
        or evidence.get("generated_close_market_fill_verified") is not True
        or evidence.get("position_count") != 0
        or evidence.get("stop_count") != 0
        or evidence.get("open_order_count") != 0
    ):
        raise pilot.TriggerPilotHalted("PREVIOUS_5BP_PILOT_MARKET_CLOSE_NOT_VERIFIED")
    bookend = await inventory_run(client=InventoryGetOnlyClient(client))
    if not _is_flat(bookend):
        raise pilot.TriggerPilotHalted("NEW_WEB_PILOT_ACCOUNT_NOT_STABLY_FLAT")
    return {
        "previous_pilot_exact_market_close_verified": True,
        "previous_pilot_account_stably_flat": True,
        "prior_sl_trigger_side_conflict_retained": evidence.get(
            "finished_trigger_side"
        ) == "TP",
        "third_journal_exists": THIRD_JOURNAL.exists(),
    }


async def preview(client: Any) -> dict[str, Any]:
    result = await pilot._preview(client)
    if (
        result.get("status") != "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED"
        or result.get("stop_bps") != str(INITIAL_STOP_BPS)
        or result.get("max_notional_usdt") != "12.50"
        or result.get("margin") != "ISOLATED"
        or result.get("leverage") != "1x"
        or result.get("contracts") != 1
    ):
        raise pilot.TriggerPilotHalted("INITIAL_AMENDMENT_PILOT_PREVIEW_INVALID")
    reference = D(result["reference_last_price"])
    tick = D(result["tick"])
    proposed = (reference * (D(1) + AMENDED_STOP_BPS / D(10000)) / tick
                ).to_integral_value(rounding=ROUND_CEILING) * tick
    if proposed >= D(result["stop_loss_price"]) or proposed <= reference:
        raise pilot.TriggerPilotHalted("AMENDMENT_TICK_RULE_NOT_ELIGIBLE")
    result["candidate_tightened_stop_price"] = str(proposed)
    result["candidate_tightened_bps"] = str(AMENDED_STOP_BPS)
    result["real_amendment_post_authorized"] = False
    return result


def _check_journal(fingerprint: str) -> dict[str, Any]:
    journal = pilot._load(fingerprint)
    if journal.get("stop_bps") != str(INITIAL_STOP_BPS):
        raise pilot.TriggerPilotHalted("THIRD_PILOT_STOP_PROFILE_MISMATCH")
    for name in ("amend_post_attempted", "amend_ack_verified"):
        if name in journal and type(journal[name]) is not bool:
            raise pilot.TriggerPilotHalted("AMENDMENT_JOURNAL_SHAPE_INVALID")
    if journal.get("amend_post_attempted") is True:
        target = pilot.positive(journal.get("amend_target_price"))
        old = pilot.positive(journal.get("amend_previous_price"))
        if (
            pilot.id_string(journal.get("amend_stop_order_id")) is None
            or target is None or old is None
            or target >= old
            or old != D(journal["stop_price"])
            or type(journal.get("amend_ack_verified")) is not bool
        ):
            raise pilot.TriggerPilotHalted("AMENDMENT_JOURNAL_PRICE_INVALID")
    elif journal.get("amend_ack_verified") is True:
        raise pilot.TriggerPilotHalted("AMENDMENT_ACK_WITHOUT_POST_FENCE")
    return journal


async def _current_ticker(client: Any) -> tuple[D, D, int]:
    quote = await PilotDryRunGetOnlyClient(client)._public_get(
        "/api/v1/contract/ticker", {"symbol": SYMBOL},
    )
    if type(quote) is list:
        rows = [row for row in quote
                if type(row) is dict and row.get("symbol") == SYMBOL]
        if len(rows) != 1:
            raise pilot.TriggerPilotHalted("AMENDMENT_TICKER_NOT_UNIQUE")
        quote = rows[0]
    if type(quote) is not dict or quote.get("symbol") != SYMBOL:
        raise pilot.TriggerPilotHalted("AMENDMENT_TICKER_INVALID")
    last = pilot.positive(quote.get("lastPrice"))
    ask = pilot.positive(quote.get("ask1"))
    stamp = quote.get("timestamp")
    now = int(time.time() * 1000)
    if (
        last is None or ask is None or type(stamp) is not int
        or stamp > now + 5000 or now - stamp > 5000
        or ask / last > D("1.002")
        or ask / last < D("0.998")
    ):
        raise pilot.TriggerPilotHalted("AMENDMENT_FRESH_QUOTE_UNVERIFIED")
    return last, ask, stamp


def candidate_amended_stop(*, reference: D, tick: D) -> D:
    if not (reference.is_finite() and reference > 0
            and tick.is_finite() and tick > 0):
        raise pilot.TriggerPilotHalted("INVALID_AMENDMENT_TICK")
    return (reference * (D(1) + AMENDED_STOP_BPS / D(10000)) / tick
            ).to_integral_value(rounding=ROUND_CEILING) * tick


def _active_stop_redacted_checks(
    *,
    position: dict[str, Any],
    stop: dict[str, Any],
    position_id: str,
    order_id: str,
) -> dict[str, bool]:
    """Redacted, strict field-by-field GET witness; no raw API values.

    These checks are the exact amendment requirements, NOT proof that an
    incomplete API row still provides safe protective market execution.
    """
    return {
        "position_id_matches_entry": (
            pilot.id_string(position.get("positionId")) == position_id
        ),
        "position_symbol_btc": position.get("symbol") == SYMBOL,
        "position_side_short": (
            type(position.get("positionType")) is int
            and position["positionType"] == 2
        ),
        "position_margin_isolated": (
            type(position.get("openType")) is int
            and position["openType"] == 1
        ),
        "position_hold_volume_one": (
            pilot.positive(position.get("holdVol")) == D(1)
        ),
        "position_leverage_one": (
            type(position.get("leverage")) in (int, float, str)
            and pilot.positive(position.get("leverage")) == D(1)
        ),
        "stop_id_present": pilot.id_string(stop.get("id")) is not None,
        "stop_position_matches_entry": (
            pilot.id_string(stop.get("positionId")) == position_id
        ),
        "stop_order_matches_entry": (
            pilot.id_string(stop.get("orderId")) == order_id
        ),
        "stop_symbol_btc": stop.get("symbol") == SYMBOL,
        "stop_side_short": (
            type(stop.get("positionType")) is int
            and stop["positionType"] == 2
        ),
        "stop_state_active": (
            type(stop.get("state")) is int
            and stop["state"] == 1
        ),
        "stop_not_finished": (
            type(stop.get("isFinished")) is int
            and stop["isFinished"] == 0
        ),
        "stop_not_triggered": (
            type(stop.get("triggerSide")) is int
            and stop["triggerSide"] == 0
        ),
        "stop_uses_latest_price": (
            type(stop.get("lossTrend")) is int
            and stop["lossTrend"] == 1
        ),
        "stop_no_reverse": (
            type(stop.get("stopLossReverse")) is int
            and stop["stopLossReverse"] == 2
        ),
        "stop_is_market_type": (
            type(stop.get("stopLossType")) is int
            and stop["stopLossType"] == 0
        ),
        "stop_error_code_zero": (
            type(stop.get("errorCode")) is int
            and stop["errorCode"] == 0
        ),
        "stop_quantity_mode_separate": (
            stop.get("profitLossVolType") == "SEPARATE"
        ),
        "stop_loss_quantity_full_one": (
            pilot.positive(stop.get("stopLossVol")) == D(1)
        ),
    }


async def _unfinished_stop_list_market_type_probe(
    client: Any, *,
    exact_stop: dict[str, Any],
    position_id: str,
    order_id: str,
) -> dict[str, Any]:
    """Independent signed GET route, fail closed if stopLossType absent.

    No POST; does not treat the UI label or `takeProfitType` as native
    stop-loss market execution proof. The unfinished TP/SL list uses the
    published historical-list endpoint rather than current open_orders.
    Only allowlisted statuses/booleans leave the account.
    """
    out: dict[str, Any] = {
        "second_official_stop_get_checked": False,
        "second_official_stop_get_exact_identity": False,
        "second_official_stop_loss_type": "UNVERIFIED",
        "second_official_stop_loss_is_market": False,
        "second_get_can_reconcile_missing_market_type": False,
    }
    try:
        response = await client._private_get(
            "/api/v1/private/stoporder/list/orders", {
                "symbol": SYMBOL,
                "is_finished": 0,
                "page_num": 1,
                "page_size": 100,
            },
        )
    except Exception:
        out["second_official_stop_get_status"] = "GET_UNAVAILABLE"
        return out
    if (
        type(response) is not list or len(response) > 100
        or any(type(row) is not dict for row in response)
    ):
        out["second_official_stop_get_status"] = "SHAPE_NOT_VERIFIED"
        return out
    out["second_official_stop_get_checked"] = True
    stop_id = pilot.id_string(exact_stop.get("id"))
    matches = [
        row for row in response
        if pilot.id_string(row.get("id")) == stop_id
    ]
    if stop_id is None or len(matches) != 1:
        out["second_official_stop_get_status"] = "EXACT_STOP_NOT_UNIQUE_ON_PAGE"
        return out
    row = matches[0]
    original_price = pilot.positive(exact_stop.get("stopLossPrice"))
    exact_identity = (
        pilot.id_string(row.get("positionId")) == position_id
        and pilot.id_string(row.get("orderId")) == order_id
        and row.get("symbol") == SYMBOL
        and type(row.get("positionType")) is int
        and row["positionType"] == 2
        and type(row.get("state")) is int
        and row["state"] == 1
        and type(row.get("isFinished")) is int
        and row["isFinished"] == 0
        and type(row.get("triggerSide")) is int
        and row["triggerSide"] == 0
        and type(row.get("errorCode")) is int
        and row["errorCode"] == 0
        and type(row.get("lossTrend")) is int
        and row["lossTrend"] == 1
        and type(row.get("stopLossReverse")) is int
        and row["stopLossReverse"] == 2
        and row.get("profitLossVolType") == "SEPARATE"
        and pilot.positive(row.get("stopLossVol")) == D(1)
        and original_price is not None
        and pilot.positive(row.get("stopLossPrice")) == original_price
    )
    out["second_official_stop_get_exact_identity"] = exact_identity
    if type(row.get("stopLossType")) is int:
        out["second_official_stop_loss_type"] = (
            "MARKET" if row["stopLossType"] == 0
            else "LIMIT" if row["stopLossType"] == 1
            else "UNRECOGNIZED"
        )
    else:
        out["second_official_stop_loss_type"] = "FIELD_NOT_REPORTED"
    out["second_official_stop_loss_is_market"] = (
        type(row.get("stopLossType")) is int and row["stopLossType"] == 0
    )
    out["second_get_can_reconcile_missing_market_type"] = (
        exact_identity and out["second_official_stop_loss_is_market"]
    )
    out["second_official_stop_get_status"] = (
        "EXACT_GET_MATCH" if exact_identity else "IDENTITY_OR_COVERAGE_MISMATCH"
    )
    return out


async def inspect(client: Any, fingerprint: str) -> dict[str, Any]:
    """Observe original/updated active native stop, always without POST."""
    journal = _check_journal(fingerprint)
    safe = pilot._GetOnly(client)
    order = await safe.get(
        f"/api/v1/private/order/external/{SYMBOL}/{journal['external_oid']}"
    )
    if type(order) is not dict or not (
        order.get("externalOid") == journal["external_oid"]
        and order.get("symbol") == SYMBOL
        and type(order.get("side")) is int and order["side"] == 3
        and type(order.get("state")) is int and order["state"] == 3
        and pilot.positive(order.get("dealVol")) == D(1)
        and pilot.positive(order.get("vol")) == D(1)
        and pilot.id_string(order.get("orderId")) is not None
        and pilot.id_string(order.get("positionId")) is not None
        and (journal.get("entry_order_id") is None or
             pilot.id_string(order.get("orderId")) == journal["entry_order_id"])
    ):
        raise pilot.TriggerPilotHalted("AMENDMENT_PILOT_ENTRY_NOT_EXACT_FILLED_SHORT")
    position_id = pilot.id_string(order["positionId"])
    order_id = pilot.id_string(order["orderId"])
    positions = await safe.get("/api/v1/private/position/open_positions")
    stops = await safe.get("/api/v1/private/stoporder/open_orders")
    counts = await safe.get("/api/v1/private/order/open_order_total_count")
    if (
        type(positions) is not list or type(stops) is not list
        or type(counts) is not dict
        or any(type(x) is not dict for x in positions + stops)
        or any(type(counts.get(k)) is not int or counts[k] < 0
               for k in ("sumCount", "stopOrderCount", "limitOrderCount",
                         "planOrderCount", "trackOrderCount"))
        or counts["stopOrderCount"] != len(stops)
        or counts["sumCount"] != sum(
            counts[k] for k in ("stopOrderCount", "limitOrderCount",
                                "planOrderCount", "trackOrderCount"))
    ):
        raise pilot.TriggerPilotHalted("AMENDMENT_CURRENT_INVENTORY_SHAPE_INVALID")
    report: dict[str, Any] = {
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "position_count": len(positions),
        "current_stop_count": len(stops),
        "open_orders": counts["sumCount"],
        "amend_post_attempted": journal.get("amend_post_attempted", False),
        "amend_ack_verified": journal.get("amend_ack_verified", False),
        "amendment_same_stop_order_verified": False,
        "amendment_full_volume_market_protection_observed": False,
        "native_amendment_live_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "exchange_mutations_by_inspect": 0,
    }
    if len(positions) == 0 and len(stops) == 0 and counts["sumCount"] == 0:
        report["status"] = "PILOT_POSITION_EXITED_RUN_ACCOUNT_FLAT_AUDIT"
        return report
    if (
        len(positions) != 1 or len(stops) != 1 or counts["sumCount"] != 1
        or counts["limitOrderCount"] != 0
        or counts["planOrderCount"] != 0
        or counts["trackOrderCount"] != 0
    ):
        report["status"] = "POSITION_OR_STOPS_NOT_UNIQUE_MANAGE_IN_MEXC"
        return report
    position, stop = positions[0], stops[0]
    checks = _active_stop_redacted_checks(
        position=position, stop=stop,
        position_id=position_id, order_id=order_id,
    )
    if not all(checks.values()):
        report["status"] = "ACTIVE_STOP_OR_POSITION_NOT_STRICT_FULL_MARKET"
        report["failed_strict_fields"] = sorted(
            name for name, passed in checks.items() if not passed
        )
        report["unreported_native_fields"] = sorted(
            name for name, field in (
                ("stop_is_market_type", "stopLossType"),
                ("stop_no_reverse", "stopLossReverse"),
                ("stop_error_code_zero", "errorCode"),
                ("stop_loss_quantity_full_one", "stopLossVol"),
                ("stop_quantity_mode_separate", "profitLossVolType"),
                ("stop_not_triggered", "triggerSide"),
            )
            if stop.get(field) is None
        )
        report["reason_detail_GET_only"] = True
        report["signed_current_stop_row_full_market_semantics_certified"] = False
        if checks["stop_is_market_type"] is False:
            report.update(await _unfinished_stop_list_market_type_probe(
                client,
                exact_stop=stop, position_id=position_id, order_id=order_id,
            ))
        return report
    current_stop = pilot.positive(stop.get("stopLossPrice"))
    original_stop = D(journal["stop_price"])
    last, ask, _ = await _current_ticker(client)
    report["current_stop_above_market"] = (
        current_stop is not None and current_stop > ask
    )
    report["original_stop_price_matches"] = current_stop == original_stop
    if journal.get("amend_post_attempted") is True:
        expected_id = pilot.id_string(journal.get("amend_stop_order_id"))
        target = pilot.positive(journal.get("amend_target_price"))
        report["amendment_same_stop_order_verified"] = (
            pilot.id_string(stop.get("id")) == expected_id
        )
        report["amendment_target_price_matches"] = current_stop == target
        report["amendment_full_volume_market_protection_observed"] = (
            report["amendment_same_stop_order_verified"]
            and report["amendment_target_price_matches"]
            and report["current_stop_above_market"]
        )
        report["status"] = (
            "NATIVE_STOP_AMENDMENT_READBACK_VERIFIED_RECONCILE_ONLY"
            if report["amendment_full_volume_market_protection_observed"]
            else "NATIVE_STOP_AMENDMENT_UNCERTAIN_OR_INCOMPLETE"
        )
    else:
        report["status"] = (
            "ONE_ATTACHED_NATIVE_MARKET_STOP_READY_FOR_AMENDMENT"
            if current_stop == original_stop and current_stop > ask
            else "ORIGINAL_NATIVE_STOP_CHANGED_OR_NOT_PROTECTIVE"
        )
    report["amendment_stop_reference_position_verified"] = True
    report["initial_stop_bps"] = str(INITIAL_STOP_BPS)
    report["target_stop_bps"] = str(AMENDED_STOP_BPS)
    # Absolute exchange IDs and prices are never printed; safe target
    # is computed again from immutable entry reference during --amend.
    return report


async def amend_once(client: Any, fingerprint: str) -> dict[str, Any]:
    journal = _check_journal(fingerprint)
    if journal.get("amend_post_attempted") is True:
        raise pilot.TriggerPilotHalted("NATIVE_STOP_AMENDMENT_ALREADY_ATTEMPTED_NO_RETRY")
    pre = await inspect(client, fingerprint)
    if pre.get("status") != "ONE_ATTACHED_NATIVE_MARKET_STOP_READY_FOR_AMENDMENT":
        raise pilot.TriggerPilotHalted("AMENDMENT_PROTECTION_PREFLIGHT_FAILED")
    pilot._confirm("TIGHTEN MY ONE BTC STOP THROUGH MEXC API ONCE")
    # Immediately recompute readback and price after operator confirmation.
    current = await inspect(client, fingerprint)
    if current.get("status") != "ONE_ATTACHED_NATIVE_MARKET_STOP_READY_FOR_AMENDMENT":
        raise pilot.TriggerPilotHalted("AMENDMENT_PROTECTION_CHANGED_AFTER_CONFIRMATION")
    # Access exact account-scoped stop id and current price once more;
    # never use or print a manually supplied ID.
    safe = pilot._GetOnly(client)
    entry = await safe.get(
        f"/api/v1/private/order/external/{SYMBOL}/{journal['external_oid']}"
    )
    stops = await safe.get("/api/v1/private/stoporder/open_orders")
    if (
        type(stops) is not list or len(stops) != 1
        or type(stops[0]) is not dict
        or type(entry) is not dict
        or pilot.id_string(stops[0].get("positionId"))
        != pilot.id_string(entry.get("positionId"))
        or pilot.id_string(stops[0].get("orderId"))
        != pilot.id_string(entry.get("orderId"))
        or pilot.positive(stops[0].get("stopLossPrice"))
        != D(journal["stop_price"])
        or type(stops[0].get("state")) is not int
        or stops[0]["state"] != 1
        or type(stops[0].get("isFinished")) is not int
        or stops[0]["isFinished"] != 0
        or type(stops[0].get("stopLossType")) is not int
        or stops[0]["stopLossType"] != 0
        or type(stops[0].get("stopLossReverse")) is not int
        or stops[0]["stopLossReverse"] != 2
        or stops[0].get("profitLossVolType") != "SEPARATE"
        or pilot.positive(stops[0].get("stopLossVol")) != D(1)
        or type(stops[0].get("triggerSide")) is not int
        or stops[0]["triggerSide"] != 0
    ):
        raise pilot.TriggerPilotHalted("AMENDMENT_FINAL_STOP_IDENTITY_CHANGED")
    stop_id = pilot.id_string(stops[0].get("id"))
    if stop_id is None:
        raise pilot.TriggerPilotHalted("AMENDMENT_STOP_ID_MISSING")
    meta = await PilotDryRunGetOnlyClient(client)._public_get(
        "/api/v1/contract/detail", {"symbol": SYMBOL},
    )
    info = one_contract_metadata(meta)
    tick = pilot.positive(info.get("priceUnit"))
    if tick is None:
        raise pilot.TriggerPilotHalted("AMENDMENT_CONTRACT_TICK_MISSING")
    reference = D(journal["reference_last_price"])
    target = candidate_amended_stop(reference=reference, tick=tick)
    old = D(journal["stop_price"])
    last, ask, _ = await _current_ticker(client)
    if (
        target >= old
        or target <= ask
        or target / ask - D(1) < MIN_STOP_TO_MARKET_BPS / D(10000)
        or old / last - D(1) > D("0.02")
        or (target / reference - D(1)) * D(10000) > D("32")
    ):
        raise pilot.TriggerPilotHalted("AMENDMENT_PRICE_NOT_SAFELY_ABOVE_MARKET")
    # Crash-safe one-shot fence. Any POST exception is UNKNOWN, never retry.
    journal["amend_post_attempted"] = True
    journal["amend_ack_verified"] = False
    journal["amend_stop_order_id"] = stop_id
    journal["amend_previous_price"] = str(old)
    journal["amend_target_price"] = str(target)
    journal["amend_post_status"] = "SUBMITTING_OR_UNKNOWN_RECONCILE_ONLY"
    pilot._persist(journal)
    print("NATIVE_AMENDMENT_JOURNALED_BEFORE_POST=true")
    print("ONE_REAL_NATIVE_AMENDMENT_POST_ATTEMPT=true")
    try:
        # Official MEXC TP/SL planned-order modification endpoint,
        # https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/
        # modify-tpsl-prices-on-a-tpsl-planned-order
        await client._private_post(AMEND_ENDPOINT, {
            "stopPlanOrderId": int(stop_id),
            "stopLossPrice": str(target),
            "lossTrend": 1,
            "stopLossReverse": 2,
        }, throttle=False)
        journal["amend_ack_verified"] = True
        journal["amend_post_status"] = "VENUE_ACK_OBSERVED_READBACK_REQUIRED"
        pilot._persist(journal)
    except BaseException:
        print("AMENDMENT_POST_OUTCOME_UNKNOWN_DO_NOT_RETRY=true")
        raise
    first = await inspect(client, fingerprint)
    second = await inspect(client, fingerprint)
    second["single_native_stop_edit_two_readbacks_verified"] = (
        journal.get("amend_ack_verified") is True
        and first.get("status") ==
            "NATIVE_STOP_AMENDMENT_READBACK_VERIFIED_RECONCILE_ONLY"
        and second.get("status") ==
            "NATIVE_STOP_AMENDMENT_READBACK_VERIFIED_RECONCILE_ONLY"
        and first.get("amendment_full_volume_market_protection_observed") is True
        and second.get("amendment_full_volume_market_protection_observed") is True
        and first.get("amendment_same_stop_order_verified") is True
        and second.get("amendment_same_stop_order_verified") is True
    )
    # Even two authenticated snapshots do not prove continuous atomicity
    # during the POST or readiness for unattended production execution.
    second["exchange_continuous_protection_certified"] = False
    print("AMENDMENT_READBACK_GET_ONLY=" + json.dumps(second, sort_keys=True))
    return second


async def execute(action: str) -> dict[str, Any] | None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("NATIVE_AMENDMENT_PILOT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    old_journal, old_bps, old_max = (
        pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS
    )
    try:
        if action in ("check", "open"):
            verified = await prior_pilot_market_close_and_flat(
                client, fingerprint,
            )
            if verified["third_journal_exists"]:
                raise pilot.TriggerPilotHalted(
                    "NATIVE_AMENDMENT_PILOT_ALREADY_JOURNALED_NO_REENTRY"
                )
        else:
            verified = None
        # Scope all original one-shot primitives to this dedicated third
        # journal and 50bp stop. The process lock prevents simultaneous CLIs
        # on the same Mac from racing the same pilot state.
        pilot.JOURNAL = THIRD_JOURNAL
        pilot.STOP_BPS = INITIAL_STOP_BPS
        pilot.MAX_BPS = MAX_INITIAL_STOP_BPS
        if action == "check":
            result = await preview(client)
            return dict(verified, initial_preview=result)
        if action == "open":
            print("EXPERIMENT=REAL_INITIAL_50BP_NATIVE_SL_THEN_30BP_AMEND")
            await pilot._send(client, fingerprint)
            return None
        if action == "inspect":
            return await inspect(client, fingerprint)
        if action == "amend":
            return await amend_once(client, fingerprint)
        if action == "close":
            await pilot._close(client, fingerprint)
            return None
        if action == "inventory":
            inventory = await inventory_run(
                client=InventoryGetOnlyClient(client),
            )
            return {
                "read_only": True,
                "account_stably_flat_verified": _is_flat(inventory),
                "position_count": inventory.get("after_positions"),
                "stop_count": inventory.get("after_current_tpsl"),
                "open_order_count": inventory.get(
                    "reported_open_order_counts", {},
                ).get("sumCount"),
                "live_authorization": False,
            }
        raise pilot.TriggerPilotHalted("AMENDMENT_ACTION_UNRECOGNIZED")
    finally:
        pilot.JOURNAL, pilot.STOP_BPS, pilot.MAX_BPS = (
            old_journal, old_bps, old_max,
        )
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time REAL BTC native attached-SL tightening test",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ("check", "open", "inspect", "amend", "close", "inventory"):
        group.add_argument("--" + action, action="store_true")
    args = parser.parse_args()
    action = next(
        name for name in ("check", "open", "inspect", "amend", "close", "inventory")
        if getattr(args, name)
    )
    try:
        with OneShotProcessLock():
            result = asyncio.run(execute(action))
        if result is not None:
            print(json.dumps(result, indent=2, sort_keys=True))
        if action in ("check", "inspect", "inventory"):
            print("GET_ONLY_NO_ORDERS=true")
    except BaseException as exc:
        reason = str(exc) if isinstance(
            exc, (pilot.TriggerPilotHalted, PilotDryRunHalted),
        ) else type(exc).__name__
        print("NATIVE_AMENDMENT_PILOT_HALTED=" + reason)
        if action in ("open", "amend", "close"):
            print("DO_NOT_RETRY_UNCERTAIN_POST_RECONCILE_IN_MEXC=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
