"""GET-only historical audit of the completed T100 WEB one-shot pilot.

No POST, no order/stop mutation, no DB, no Render, no BOXWAVE, no credentials
or exchange IDs in output. Matches exact entry and close by journaled OIDs.
Finished TP/SL pages do not report pagination totals: matching rows can be
observed, but negative results NEVER certify absence or full history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, read_pilot_keychain_credentials,
    _run as run_inventory,
)
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, OneShotHalted, _allow_real_mode_env,
    _load_journal,
)

SYMBOL = "BTC_USDT"
PAGE_SIZE = 100
MAX_PAGES = 3


class FinishedStopForensicsHalted(ValueError):
    pass


def _id(value: Any) -> str | None:
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]*", value) else None


def _positive(value: Any) -> Decimal | None:
    if type(value) is bool or type(value) not in (str, float, int, Decimal):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _enum_status(raw: Any, names: dict[int, str]) -> str:
    """Classify a documented numeric venue field without leaking raw rows.

    Missing is NOT the same as a contradictory value; booleans and text do
    not silently compare equal to integer 0 in a Python venue response.
    """
    if raw is None:
        return "NOT_REPORTED"
    if type(raw) is not int:
        return "UNEXPECTED_TYPE"
    return names.get(raw, "UNRECOGNIZED_VALUE")


def _error_code_status(raw: Any) -> str:
    if raw is None:
        return "NOT_REPORTED"
    if type(raw) is not int:
        return "UNEXPECTED_TYPE"
    if raw == 0:
        return "ZERO_NORMAL"
    if raw > 0:
        return "NONZERO_REPORTED"
    return "NEGATIVE_INVALID"


def _quantity_mode_status(raw: Any) -> str:
    if raw is None:
        return "NOT_REPORTED"
    if type(raw) is not str:
        return "UNEXPECTED_TYPE"
    return {
        "SAME": "SAME",
        "SEPARATE": "SEPARATE",
    }.get(raw, "UNRECOGNIZED_VALUE")


def _is_flat(book: Any) -> bool:
    return (
        type(book) is dict
        and book.get("account_reconciled") is True
        and book.get("account_wide_completeness_verified") is True
        and book.get("critical_inventory_equal_across_witness_window") is True
        and all(
            type(book.get(k)) is int and book[k] == 0
            for k in ("before_positions", "after_positions",
                      "before_current_tpsl", "after_current_tpsl")
        )
        and type(book.get("reported_open_order_counts")) is dict
        and all(
            type(book["reported_open_order_counts"].get(k)) is int
            and book["reported_open_order_counts"][k] == 0
            for k in ("sumCount", "limitOrderCount", "stopOrderCount",
                      "planOrderCount", "trackOrderCount")
        )
    )


def assess_closed_pilot_finished_stop(
    *,
    journal: Any, entry_order: Any, close_order: Any,
    finished_rows: Any, flat_inventory: Any, pages_read: int,
) -> dict[str, Any]:
    """Pure, conservative forensic report, never a venue mutation permit."""
    base: dict[str, Any] = {
        "status": "PILOT_FINISHED_STOP_EVIDENCE_UNVERIFIED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "account_bookend_flat_verified": _is_flat(flat_inventory),
        "entry_filled_verified": False,
        "close_filled_verified": False,
        "attached_entry_stop_terminal_observed": False,
        "native_stop_triggered_verified": False,
        "native_stop_execution_certified": False,
        "finished_stop_history_complete": False,
        "stop_amendment_supported_on_venue": False,
        "replay_live_authorization": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": 0,
        "exchange_mutations": 0,
        "manual_positions_adopted": False,
        "pages_read": pages_read,
    }

    if (
        type(journal) is not dict
        or journal.get("symbol") != SYMBOL
        or journal.get("entry_post_attempted") is not True
        or journal.get("entry_ack_verified") is not True
        or journal.get("close_post_attempted") is not True
        or journal.get("close_ack_verified") is not True
        or type(journal.get("draft")) is not dict
        or type(journal.get("external_oid")) is not str
        or journal["draft"].get("externalOid") != journal["external_oid"]
        or type(journal.get("close_oid")) is not str
        or not re.fullmatch(r"TPWEB[a-f0-9]{24}", journal["external_oid"])
        or not re.fullmatch(r"TPWEB[a-f0-9]{24}", journal["close_oid"])
        or journal["close_oid"] == journal["external_oid"]
        or type(entry_order) is not dict
        or type(close_order) is not dict
        or type(finished_rows) is not list
        or any(type(row) is not dict for row in finished_rows)
        or type(pages_read) is not int or not 1 <= pages_read <= MAX_PAGES
    ):
        base["status"] = "PILOT_JOURNAL_OR_VENUE_RESPONSE_UNVERIFIED"
        return base

    entry_id = _id(entry_order.get("orderId"))
    close_id = _id(close_order.get("orderId"))
    position_id = _id(entry_order.get("positionId"))
    if not entry_id or not close_id or not position_id or close_id == entry_id:
        base["status"] = "PILOT_ORDER_IDS_UNVERIFIED"
        return base

    # Order provenance is exact: nothing on the old/manual account can be
    # selected by symbol alone. A matched externalOid and positionId are
    # required, along with each order's actual full 1-contract execution.
    entry_ok = (
        entry_order.get("symbol") == SYMBOL
        and entry_order.get("externalOid") == journal["external_oid"]
        and entry_order.get("side") == 3
        and entry_order.get("state") == 3
        and entry_order.get("openType") == 1
        and _positive(entry_order.get("dealVol")) == Decimal(1)
        and _positive(entry_order.get("vol")) == Decimal(1)
    )
    close_ok = (
        close_order.get("symbol") == SYMBOL
        and close_order.get("externalOid") == journal["close_oid"]
        and close_order.get("side") == 2
        and close_order.get("state") == 3
        and _id(close_order.get("positionId")) == position_id
        and _positive(close_order.get("dealVol")) == Decimal(1)
        and _positive(close_order.get("vol")) == Decimal(1)
    )
    base["entry_filled_verified"] = entry_ok
    base["close_filled_verified"] = close_ok
    if not entry_ok or not close_ok or not _is_flat(flat_inventory):
        base["status"] = "PILOT_ENTRY_CLOSE_OR_FLATNESS_UNVERIFIED"
        return base

    # Exact order-linked historical row; a position-linked row with a
    # different orderId is NOT sufficient evidence for our own embedded SL.
    candidates = [
        row for row in finished_rows
        if row.get("symbol") == SYMBOL
        and _id(row.get("positionId")) == position_id
        and _id(row.get("orderId")) == entry_id
    ]
    if not candidates:
        base["status"] = "FINISHED_EMBEDDED_STOP_NOT_FOUND_IN_SCOPED_PAGES"
        return base
    stop_ids = [_id(r.get("id")) for r in candidates]
    if len(candidates) != 1 or None in stop_ids or len(set(stop_ids)) != len(stop_ids):
        base["status"] = "FINISHED_EMBEDDED_STOP_AMBIGUOUS"
        return base

    row = candidates[0]
    expected_stop = _positive(journal["draft"].get("stopLossPrice"))
    actual_stop = _positive(row.get("stopLossPrice"))
    if (
        expected_stop is None or actual_stop != expected_stop
        or type(row.get("positionType")) is not int
        or row["positionType"] != 2
        or row.get("lossTrend") != 1
        or row.get("isFinished") != 1
        or row.get("state") not in (2, 3, 4, 5)
        or type(row.get("triggerSide")) is not int
        or row["triggerSide"] not in (0, 1, 2)
    ):
        base["status"] = "FINISHED_EMBEDDED_STOP_FIELDS_UNVERIFIED"
        return base

    base["attached_entry_stop_terminal_observed"] = True
    base["terminal_state"] = {
        2: "CANCELED",
        3: "EXECUTED",
        4: "INVALIDATED",
        5: "EXECUTION_FAILED",
    }[row["state"]]
    base["trigger_side"] = {
        0: "NOT_TRIGGERED", 1: "TAKE_PROFIT", 2: "STOP_LOSS",
    }[row["triggerSide"]]
    # A `false` field below is only an UNVERIFIED observation. The
    # additional status labels differentiate MEXC omitting historical
    # metadata from MEXC actually returning a conflicting value.
    base["stop_loss_type_status"] = _enum_status(
        row.get("stopLossType"), {0: "MARKET", 1: "LIMIT"},
    )
    base["volume_type_status"] = _enum_status(
        row.get("volType"), {1: "PARTIAL", 2: "ENTIRE_POSITION"},
    )
    base["profit_loss_volume_mode_status"] = _quantity_mode_status(
        row.get("profitLossVolType"),
    )
    base["error_code_status"] = _error_code_status(
        row.get("errorCode"),
    )
    base["stop_loss_reverse_status"] = _enum_status(
        row.get("stopLossReverse"), {1: "REVERSE", 2: "NO_REVERSE"},
    )
    base["market_stop_type_observed"] = (
        base["stop_loss_type_status"] == "MARKET"
    )
    base["no_reverse_observed"] = (
        base["stop_loss_reverse_status"] == "NO_REVERSE"
    )
    base["full_position_mode_observed"] = (
        base["volume_type_status"] == "ENTIRE_POSITION"
        and base["profit_loss_volume_mode_status"] == "SAME"
    )
    base["no_error_reported"] = (
        base["error_code_status"] == "ZERO_NORMAL"
    )

    if row["state"] in (2, 4) and row["triggerSide"] == 0:
        base["status"] = "ORDER_ATTACHED_STOP_TERMINAL_UNTRIGGERED_OBSERVED"
    elif row["state"] == 3 and row["triggerSide"] == 2:
        base["status"] = "STOP_TRIGGER_ROW_SEEN_BUT_EXECUTION_NOT_CERTIFIED"
    elif row["state"] == 5:
        base["status"] = "STOP_EXECUTION_FAILURE_REPORTED"
    else:
        base["status"] = "STOP_TERMINAL_EVENT_REQUIRES_REVIEW"
    return base


class FinishedStopGetOnly:
    """Expose only specific authenticated GET paths; no POST method."""
    __slots__ = ("__client",)

    def __init__(self, client: Any):
        self.__client = client

    async def get_order(self, oid: str) -> Any:
        if type(oid) is not str or re.fullmatch(r"TPWEB[a-f0-9]{24}", oid) is None:
            raise FinishedStopForensicsHalted("ORDER_ORIGIN_INVALID")
        return await self.__client._private_get(
            f"/api/v1/private/order/external/{SYMBOL}/{oid}"
        )

    async def get_stop_page(self, page: int) -> list[dict[str, Any]]:
        if type(page) is not int or not 1 <= page <= MAX_PAGES:
            raise FinishedStopForensicsHalted("STOP_PAGE_INVALID")
        rows = await self.__client._private_get(
            "/api/v1/private/stoporder/list/orders",
            {"symbol": SYMBOL, "page_num": page,
             "page_size": PAGE_SIZE, "is_finished": 1},
        )
        if type(rows) is not list or len(rows) > PAGE_SIZE or any(
            type(row) is not dict for row in rows
        ):
            raise FinishedStopForensicsHalted("STOP_PAGE_SCHEMA_UNVERIFIED")
        return rows


async def audit(client: Any, journal: dict[str, Any]) -> dict[str, Any]:
    safe = FinishedStopGetOnly(client)
    # Bookend before & after finished history observation. Both independent
    # inventory reports must be reconciled and flat before any result.
    first = await run_inventory(client=InventoryGetOnlyClient(client))
    entry = await safe.get_order(journal["external_oid"])
    close = await safe.get_order(journal["close_oid"])
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    for page in range(1, MAX_PAGES + 1):
        part = await safe.get_stop_page(page)
        pages += 1
        for row in part:
            # Duplicates across pages indicate shifting page boundaries or
            # unexpected venue behavior: do not classify an unstable result.
            sid = _id(row.get("id"))
            if sid is None or sid in seen:
                raise FinishedStopForensicsHalted("STOP_HISTORY_DUPLICATED_OR_UNKEYED")
            seen.add(sid)
            rows.append(row)
        if len(part) < PAGE_SIZE:
            break
    second = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(first) or not _is_flat(second):
        raise FinishedStopForensicsHalted("BOOKENDED_FLAT_ACCOUNT_UNVERIFIED")
    result = assess_closed_pilot_finished_stop(
        journal=journal, entry_order=entry,
        close_order=close, finished_rows=rows,
        flat_inventory=second, pages_read=pages,
    )
    result["flat_before_and_after_history_verified"] = True
    return result


async def _main() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise FinishedStopForensicsHalted("MAC_REQUIRED")
    _allow_real_mode_env()
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    journal = _load_journal(fingerprint)
    if not journal.get("close_post_attempted"):
        raise FinishedStopForensicsHalted("PILOT_CLOSE_NOT_ATTEMPTED")
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient("https://api.mexc.com", api_key=key, api_secret=secret)
    try:
        return await asyncio.wait_for(audit(client, journal), timeout=70)
    finally:
        await client.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="GET-only completed T100 web pilot stop-history audit",
    )
    parser.parse_args()   # No flags/sending escape hatch
    try:
        with OneShotProcessLock():
            print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
        print("GET_ONLY. NO ORDERS. NO LIVE AUTHORIZATION.")
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, (FinishedStopForensicsHalted, OneShotHalted))
            else type(exc).__name__
        )
        print(json.dumps({
            "status": "FINISHED_STOP_FORENSICS_HALTED",
            "reason": reason,
            "mexc_orders_sent": 0,
            "live_authorization": False,
        }, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
