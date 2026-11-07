"""MEXC signed-GET-only exact closed-trade forensic audit for third T100 pilot.

No order submission, cancellation, amendment, journal mutation, credential
printing, manual-position adoption, or production authorization. Replays the
existing dedicated third-pilot entry journal, queries the venue's actual
historical FILLED orders, independently GETs any exact matching close order
and its deals, and bookends the full dedicated subaccount inventory.

MEXC docs: GET /api/v1/private/order/list/history_orders (states=3),
GET /api/v1/private/order/get/{orderId},
GET /api/v1/private/order/deal_details/{orderId}.
The historical API documentation contradicts itself on whether `data`
contains an array or a metadata wrapper, so incomplete pages are never
promoted to a claim of exhaustive account history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from decimal import Decimal
from typing import Any

from app import t100_web_pilot_tight_stop_trigger_cli as pilot
from app import t100_web_pilot_native_stop_amendment_cli as native
from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as run_inventory,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)

D = Decimal
MAX_PAGES = 3
PAGE_SIZE = 100
HISTORY_PATH = "/api/v1/private/order/list/history_orders"


class HistoryGetOnly:
    """Explicit GET route and argument allowlist; no mutation API."""
    __slots__ = ("__client",)

    def __init__(self, client: Any):
        self.__client = client

    async def get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> Any:
        if path == HISTORY_PATH:
            if (
                type(params) is not dict
                or set(params) != {"symbol", "states", "page_num", "page_size"}
                or params["symbol"] != pilot.SYMBOL
                or params["states"] != "3"
                or type(params["page_num"]) is not int
                or params["page_num"] not in range(1, MAX_PAGES + 1)
                or type(params["page_size"]) is not int
                or params["page_size"] != PAGE_SIZE
            ):
                raise pilot.TriggerPilotHalted("CLOSE_HISTORY_PARAMETERS_INVALID")
        elif (
            re.fullmatch(
                r"/api/v1/private/order/(?:get|deal_details)/[1-9][0-9]*", path
            ) is None
            and re.fullmatch(
                r"/api/v1/private/order/external/BTC_USDT/TPWEB[a-f0-9]{24}",
                path,
            ) is None
        ) or params is not None:
            raise pilot.TriggerPilotHalted("CLOSE_AUDIT_GET_ROUTE_NOT_ALLOWED")
        return await self.__client._private_get(path, params)


def _parse_page(data: Any, page: int) -> tuple[list[dict[str, Any]], bool]:
    """Return rows and documented last-page proof, if available."""
    complete = False
    if type(data) is dict:
        rows = data.get("resultList")
        current = data.get("currentPage")
        total = data.get("totalPage")
        size = data.get("pageSize")
        count = data.get("totalCount")
        if (
            type(current) is not int or current != page
            or type(total) is not int or total < page
            or type(size) is not int or size != PAGE_SIZE
            or type(count) is not int or count < 0
            or type(rows) is not list
        ):
            raise pilot.TriggerPilotHalted("HISTORY_PAGINATION_METADATA_UNVERIFIED")
        complete = page == total
    elif type(data) is list:
        rows = data
    else:
        raise pilot.TriggerPilotHalted("HISTORY_DATA_NOT_ARRAY_OR_PAGE")
    if (
        len(rows) > PAGE_SIZE
        or any(type(r) is not dict for r in rows)
    ):
        raise pilot.TriggerPilotHalted("HISTORY_ROWS_MALFORMED")
    # A short bare array only proves that this page ended. It does NOT
    # demonstrate the venue exposes a complete historical archive.
    return rows, complete


async def _filled_candidates(
    safe: HistoryGetOnly, *, position_id: str, entry_order_id: str,
) -> tuple[list[str], int, bool, bool]:
    candidates: list[str] = []
    seen: set[str] = set()
    pages_read = 0
    history_metadata_complete = False
    history_partial = False
    for page in range(1, MAX_PAGES + 1):
        response = await safe.get(HISTORY_PATH, {
            "symbol": pilot.SYMBOL, "states": "3",
            "page_num": page, "page_size": PAGE_SIZE,
        })
        rows, complete = _parse_page(response, page)
        pages_read += 1
        for row in rows:
            oid = pilot.id_string(row.get("orderId"))
            if oid is None or oid in seen:
                raise pilot.TriggerPilotHalted("HISTORY_ORDER_ID_MISSING_OR_DUPLICATED")
            seen.add(oid)
            if (
                pilot.id_string(row.get("positionId")) == position_id
                and row.get("symbol") == pilot.SYMBOL
                and type(row.get("side")) is int
                and row["side"] == 2
                and type(row.get("state")) is int
                and row["state"] == 3
                and oid != entry_order_id
            ):
                candidates.append(oid)
        if complete:
            history_metadata_complete = True
            break
        if len(rows) < PAGE_SIZE:
            break
        if page == MAX_PAGES:
            history_partial = True
    return candidates, pages_read, history_metadata_complete, history_partial


async def _verify_exact_close(
    safe: HistoryGetOnly, *,
    close_order_id: str, entry_position_id: str,
    journal: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exact_close_order_filled": False,
        "exact_close_trade_deals_filled": False,
        "close_market_order_type_verified": False,
        "taker_close_trade_observed": False,
        "closed_contract_quantity_exact_one": False,
        "close_origin": "UNKNOWN_OR_EXTERNAL",
        "close_deal_count": 0,
        "close_stop_origin_certified": False,
        "close_api_error": "NOT_VERIFIED",
    }
    row = await safe.get(
        f"/api/v1/private/order/get/{close_order_id}",
    )
    if type(row) is not dict or not (
        pilot.id_string(row.get("orderId")) == close_order_id
        and pilot.id_string(row.get("positionId")) == entry_position_id
        and row.get("symbol") == pilot.SYMBOL
        and type(row.get("side")) is int and row["side"] == 2
        and type(row.get("state")) is int and row["state"] == 3
        and pilot.positive(row.get("dealVol")) == D(1)
        and pilot.positive(row.get("vol")) == D(1)
        and row.get("openType") == 1
    ):
        return result
    if type(row.get("errorCode")) is int and row["errorCode"] != 0:
        result["close_api_error"] = "NONZERO"
        return result
    result["close_api_error"] = (
        "ZERO" if type(row.get("errorCode")) is int
        and row["errorCode"] == 0 else "NOT_REPORTED"
    )
    result["exact_close_order_filled"] = True
    result["close_market_order_type_verified"] = (
        type(row.get("orderType")) is int and row["orderType"] == 5
    )
    if (
        journal.get("close_post_attempted") is True
        and journal.get("close_oid") is not None
        and journal.get("close_oid") == row.get("externalOid")
    ):
        result["close_origin"] = "MATCHES_THIRD_PILOT_SCRIPT_CLOSE"
    # If the user closed in MEXC UI or an SL closed it, externalOid can
    # differ. Never infer exact origin from matching prices/position alone.
    fills = await safe.get(
        f"/api/v1/private/order/deal_details/{close_order_id}",
    )
    if (
        type(fills) is not list or not fills or len(fills) > PAGE_SIZE
        or any(type(x) is not dict for x in fills)
    ):
        return result
    total = D(0)
    ids: set[str] = set()
    taker = False
    for fill in fills:
        trade_id = pilot.id_string(fill.get("id"))
        vol = pilot.positive(fill.get("vol"))
        price = pilot.positive(fill.get("price"))
        liquidity = fill.get("taker", fill.get("isTaker"))
        if (
            trade_id is None or trade_id in ids
            or pilot.id_string(fill.get("orderId")) != close_order_id
            or fill.get("symbol") != pilot.SYMBOL
            or type(fill.get("side")) is not int or fill["side"] != 2
            or vol is None or price is None
            or type(fill.get("timestamp")) is not int
            or fill["timestamp"] <= 0
            or type(liquidity) is not bool
        ):
            return result
        ids.add(trade_id)
        total += vol
        taker = taker or liquidity
    result["close_deal_count"] = len(ids)
    result["closed_contract_quantity_exact_one"] = total == D(1)
    result["exact_close_trade_deals_filled"] = (
        total == D(1) and result["exact_close_order_filled"]
    )
    result["taker_close_trade_observed"] = taker
    return result


async def audit_third_pilot(client: Any, fingerprint: str) -> dict[str, Any]:
    """Reconstruct one real trade from third immutable journal + signed GET."""
    prior_path = pilot.JOURNAL
    try:
        pilot.JOURNAL = native.THIRD_JOURNAL
        journal = native._check_journal(fingerprint)
    finally:
        pilot.JOURNAL = prior_path
    report: dict[str, Any] = {
        "status": "CLOSE_AUDIT_NONCERTIFYING",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "third_pilot_entry_filled_verified": False,
        "account_bookended_flat_verified": False,
        "filled_close_candidate_count": 0,
        "all_historical_orders_complete": False,
        "history_result_page_limit_reached": False,
        "historical_pages_read": 0,
        "native_sl_trigger_origin_certified": False,
        "native_amendment_live_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent": 0,
        "read_only": True,
    }
    safe = HistoryGetOnly(client)
    before = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(before):
        report["status"] = "ACCOUNT_NOT_FLAT_CLOSE_AUDIT_HALTED"
        return report
    entry = await safe.get(
        f"/api/v1/private/order/external/{pilot.SYMBOL}/{journal['external_oid']}"
    )
    position_id = (
        pilot.id_string(entry.get("positionId"))
        if type(entry) is dict else None
    )
    entry_id = (
        pilot.id_string(entry.get("orderId"))
        if type(entry) is dict else None
    )
    if not (
        type(entry) is dict
        and position_id is not None and entry_id is not None
        and entry.get("externalOid") == journal["external_oid"]
        and entry.get("symbol") == pilot.SYMBOL
        and type(entry.get("side")) is int and entry["side"] == 3
        and type(entry.get("state")) is int and entry["state"] == 3
        and type(entry.get("openType")) is int
        and entry["openType"] == 1
        and pilot.positive(entry.get("dealVol")) == D(1)
        and pilot.positive(entry.get("vol")) == D(1)
        and (
            journal.get("entry_ack_verified") is not True
            or entry_id == journal.get("entry_order_id")
        )
    ):
        report["status"] = "THIRD_ENTRY_OR_ORIGIN_NOT_EXACT"
        return report
    report["third_pilot_entry_filled_verified"] = True
    order_ids, pages, metadata_complete, history_partial = (
        await _filled_candidates(
            safe, position_id=position_id, entry_order_id=entry_id,
        )
    )
    report["filled_close_candidate_count"] = len(order_ids)
    report["all_historical_orders_complete"] = metadata_complete
    report["history_result_page_limit_reached"] = history_partial
    report["historical_pages_read"] = pages
    if history_partial:
        report["status"] = "HISTORY_PAGE_LIMIT_REACHED_RECONCILE_MANUALLY"
        return report
    if len(order_ids) != 1:
        report["status"] = (
            "MULTIPLE_FILLED_CLOSE_ORDERS_RECONCILE_MANUALLY"
            if len(order_ids) > 1
            else "EXACT_CLOSE_NOT_FOUND_IN_BOUNDED_HISTORY"
        )
        return report

    verified = await _verify_exact_close(
        safe, close_order_id=order_ids[0],
        entry_position_id=position_id, journal=journal,
    )
    report.update(verified)
    after = await run_inventory(client=InventoryGetOnlyClient(client))
    report["account_bookended_flat_verified"] = (
        _is_flat(before) and _is_flat(after)
    )
    if not report["account_bookended_flat_verified"]:
        report["status"] = "ACCOUNT_FLAT_DRIFTED_DURING_AUDIT"
    elif (
        verified["exact_close_order_filled"]
        and verified["exact_close_trade_deals_filled"]
        and verified["closed_contract_quantity_exact_one"]
    ):
        report["status"] = (
            "THIRD_ONE_CONTRACT_REAL_CLOSE_AND_FLAT_VERIFIED_NONAUTHORIZING"
        )
    else:
        report["status"] = "THIRD_CLOSE_FILL_NOT_INDEPENDENTLY_VERIFIED"
    return report


async def _main() -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise pilot.TriggerPilotHalted("THIRD_AUDIT_MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        result = await audit_third_pilot(client, fingerprint)
        print(json.dumps(result, indent=2, sort_keys=True))
        print("SIGNED_GET_ONLY_NO_ORDERS_OR_JOURNAL_MUTATIONS=true")
    finally:
        await client.close()


def main() -> None:
    try:
        with OneShotProcessLock():
            asyncio.run(_main())
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, pilot.TriggerPilotHalted) else type(exc).__name__
        print("THIRD_CLOSE_AUDIT_HALTED=" + reason)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
