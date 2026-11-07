"""Dedicated ONE-SHOT real tight-stop trigger experiment, new T100 WEB pilot only.

A NEW minimum-size short is needed because the earlier pilot is flat.
--check is GET only. --send explicitly confirms ONE exchange POST with a
15-basis-point embedded short stop (0.15% above latest-price reference).
--status / --watch are GET only. --close explicitly confirms one close POST
for the exact owned position, if still open. Neither POST ever retries.
The native stop MAY FAIL TO ATTACH, TRIGGER OR FILL. An order ACK proves
none of these. Never run unattended. No Render, DB, BOXWAVE, or full-capital
authorization. Do not delete the journal to repeat this pilot.
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
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, _run as run_inventory,
    read_pilot_keychain_credentials,
)
from app.t100_web_pilot_dry_run_cli import (
    PilotDryRunGetOnlyClient, check as preflight,
    decimal, one_contract_metadata,
)
from app.t100_web_pilot_one_shot_cli import (
    OneShotProcessLock, _allow_real_mode_env,
)
from app.t100_web_pilot_finished_stop_audit_cli import _is_flat

D = Decimal
BASE_DIR = Path.home() / "Library" / "Application Support" / "T100WebPilot"
JOURNAL = BASE_DIR / "tight_stop_trigger_001.json"
SYMBOL = "BTC_USDT"
STOP_BPS = D(15)
MAX_BPS = D(17)
MAX_NOTIONAL = D("12.50")
MAX_PAGES = 3
MAX_WATCH_SECONDS = 240
OID_RE = re.compile(r"TPWEB[a-f0-9]{24}\Z")
ID_RE = re.compile(r"[1-9][0-9]*\Z")
OPEN_PATH = "/api/v1/private/order/create"


class TriggerPilotHalted(RuntimeError):
    pass


def id_string(value: Any) -> str | None:
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if ID_RE.fullmatch(value) else None


def positive(value: Any) -> D | None:
    try:
        if type(value) not in (int, str, float, D):
            return None
        result = D(str(value))
        return result if result.is_finite() and result > 0 else None
    except (TypeError, ValueError, ArithmeticError):
        return None


def _sync_dir() -> None:
    fd = os.open(BASE_DIR, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_directory() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if BASE_DIR.is_symlink() or JOURNAL.is_symlink():
        raise TriggerPilotHalted("UNSAFE_JOURNAL_PATH")
    os.chmod(BASE_DIR, 0o700)


def _write_first(body: dict[str, Any]) -> None:
    _safe_directory()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(JOURNAL, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, sort_keys=True, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    _sync_dir()


def _persist(body: dict[str, Any]) -> None:
    _safe_directory()
    if not JOURNAL.is_file():
        raise TriggerPilotHalted("JOURNAL_NOT_FOUND")
    fd, path = tempfile.mkstemp(dir=BASE_DIR, prefix=".tight-stop-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(path, JOURNAL)
        _sync_dir()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _load(fingerprint: str) -> dict[str, Any]:
    if JOURNAL.is_symlink() or not JOURNAL.is_file():
        raise TriggerPilotHalted("TIGHT_PILOT_JOURNAL_MISSING")
    if JOURNAL.stat().st_mode & 0o077:
        raise TriggerPilotHalted("UNSAFE_JOURNAL_PERMISSIONS")
    body = json.loads(JOURNAL.read_text(encoding="utf-8"))
    if (
        type(body) is not dict
        or body.get("version") != 1
        or body.get("credential_scope") != "T100_WEB_PILOT_SUBACCOUNT"
        or body.get("key_sha256") != fingerprint
        or body.get("symbol") != SYMBOL
        or not isinstance(body.get("external_oid"), str)
        or not OID_RE.fullmatch(body["external_oid"])
        or body.get("contracts") != 1
        or body.get("open_type") != 1
        or body.get("stop_bps") != "15"
        or positive(body.get("stop_price")) is None
        or type(body.get("entry_post_attempted")) is not bool
        or type(body.get("close_post_attempted")) is not bool
    ):
        raise TriggerPilotHalted("JOURNAL_ACCOUNT_OR_SHAPE_MISMATCH")
    return body


def _confirm(phrase: str) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TriggerPilotHalted("INTERACTIVE_TERMINAL_REQUIRED")
    print("REAL BTC_USDT FUTURES ORDER. Embedded SL MAY FAIL to trigger or fill.")
    print("Keep MEXC web UI open. Do not leave an unprotected position unattended.")
    if input(f"Type exactly '{phrase}': ").strip() != phrase:
        raise TriggerPilotHalted("OPERATOR_CONFIRMATION_DECLINED")


async def _preview(client: Any) -> dict[str, Any]:
    result = await asyncio.wait_for(
        preflight(PilotDryRunGetOnlyClient(client)), timeout=55,
    )
    if (
        result.get("status") != "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED"
        or result.get("credential_scope") != "T100_WEB_PILOT_SUBACCOUNT"
        or result.get("notional_le_25_pct_observed_available") is not True
        or result.get("contracts") != 1
        or result.get("margin") != "ISOLATED"
        or result.get("leverage") != "1x"
        or result.get("entry_allowed") is not False
        or result.get("live_authorization") is not False
        or result.get("mexc_orders_sent") != 0
    ):
        raise TriggerPilotHalted("TIGHT_STOP_PREFLIGHT_NOT_ELIGIBLE")
    meta = await PilotDryRunGetOnlyClient(client)._public_get(
        "/api/v1/contract/detail", {"symbol": SYMBOL},
    )
    tick = decimal(one_contract_metadata(meta).get("priceUnit"))
    last = decimal(result.get("reference_last_price"))
    notional = decimal(result.get("indicative_notional_usdt"))
    if notional > MAX_NOTIONAL:
        raise TriggerPilotHalted("NOTIONAL_OVER_CAP")
    stop = (last * (D(1) + STOP_BPS / D(10000)) / tick).to_integral_value(
        rounding=ROUND_CEILING,
    ) * tick
    # Rounding must be sane and the short protective stop must not have
    # been reached already. A 15bps stop has no guaranteed loss ceiling.
    if (
        stop <= last
        or (stop / last - D(1)) * D(10000) > MAX_BPS
        or stop <= last + tick * 2
        or stop * D("0.9992") <= last
    ):
        raise TriggerPilotHalted("TIGHT_STOP_TICK_OR_MARKET_INVALID")
    return {
        "status": "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED",
        "reference_last_price": str(last),
        "stop_loss_price": str(stop),
        "tick": str(tick),
        "stop_bps": str(STOP_BPS),
        "notional_usdt": str(notional),
        "contracts": 1,
        "margin": "ISOLATED",
        "leverage": "1x",
        "max_notional_usdt": str(MAX_NOTIONAL),
        "max_share_available": "25%",
        "mexc_orders_sent": 0,
        "live_authorization": False,
    }


def _entry_body(data: dict[str, Any], oid: str) -> dict[str, Any]:
    if type(oid) is not str or not OID_RE.fullmatch(oid):
        raise TriggerPilotHalted("ORDER_ORIGIN_INVALID")
    if data.get("status") != "TIGHT_STOP_GET_ONLY_PREVIEW_NOT_AUTHORIZED":
        raise TriggerPilotHalted("ENTRY_PREVIEW_INVALID")
    return {
        "symbol": SYMBOL,
        "price": data["reference_last_price"],
        "vol": 1, "leverage": 1, "side": 3, "type": 5,
        "openType": 1, "positionMode": 1,
        "externalOid": oid,
        "stopLossPrice": data["stop_loss_price"],
        "lossTrend": 1,
    }


async def _send(client: Any, fingerprint: str) -> None:
    _safe_directory()
    if JOURNAL.exists() or JOURNAL.is_symlink():
        raise TriggerPilotHalted("TIGHT_STOP_PILOT_ALREADY_JOURNALED_NO_RETRY")
    before = await _preview(client)
    print("GET_ONLY_PREFLIGHT=" + json.dumps(before, sort_keys=True))
    _confirm("PLACE ONE TIGHT STOP BTC SHORT")
    fresh = await _preview(client)
    old_price = D(before["reference_last_price"])
    new_price = D(fresh["reference_last_price"])
    if (
        abs(new_price / old_price - D(1)) > D("0.0025")
        or fresh["tick"] != before["tick"]
    ):
        raise TriggerPilotHalted("MARKET_MOVED_OR_TICK_CHANGED_RECHECK")
    oid = "TPWEB" + secrets.token_hex(12)
    draft = _entry_body(fresh, oid)
    body = {
        "version": 1,
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "key_sha256": fingerprint,
        "symbol": SYMBOL,
        "external_oid": oid,
        "contracts": 1,
        "open_type": 1,
        "stop_price": draft["stopLossPrice"],
        "stop_bps": str(STOP_BPS),
        "reference_last_price": draft["price"],
        "entry_post_attempted": True,
        "entry_ack_verified": False,
        "entry_order_id": None,
        "close_post_attempted": False,
        "close_oid": None,
        "close_ack_verified": False,
        "status": "ENTRY_POST_UNCERTAIN_NO_RETRY",
    }
    _write_first(body)  # Durable, exclusive BEFORE real POST.
    print("ENTRY_POST_FENCED=true; ONE_REAL_ORDER_ATTEMPT=true")
    try:
        result = await client._private_post(OPEN_PATH, draft, throttle=False)
        order_id = id_string(result.get("orderId")) if type(result) is dict else None
        if order_id is None:
            raise TriggerPilotHalted("ENTRY_ACK_ID_UNKNOWN_NO_RETRY")
        body["entry_ack_verified"] = True
        body["entry_order_id"] = order_id
        body["status"] = "ENTRY_ACKED_STOP_TRIGGER_MONITOR_REQUIRED"
        _persist(body)
    except BaseException:
        print("ENTRY_OUTCOME_UNCERTAIN_CHECK_MEXC_UI_DO_NOT_RETRY=true")
        raise
    print("ENTRY_ACK_VERIFIED=true")
    print("URGENT_MONITOR_NATIVE_SL_IN_MEXC=true")
    print("Run --watch or --status; use --close if the trigger does not occur.")
    report = await _status(client, fingerprint)
    print("INITIAL_POSTFLIGHT=" + json.dumps(report, sort_keys=True))


class _GetOnly:
    __slots__ = ("__client",)

    def __init__(self, client: Any):
        self.__client = client

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
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
            and re.fullmatch(r"/api/v1/private/order/get/[1-9][0-9]*", path) is None
            and path != "/api/v1/private/stoporder/list/orders"
        ):
            raise TriggerPilotHalted("PRIVATE_GET_ENDPOINT_NOT_ALLOWED")
        if path == "/api/v1/private/stoporder/list/orders":
            if (
                type(params) is not dict
                or params.get("symbol") != SYMBOL
                or params.get("is_finished") != 1
                or params.get("page_size") != 100
                or type(params.get("page_num")) is not int
                or params["page_num"] not in range(1, MAX_PAGES + 1)
            ):
                raise TriggerPilotHalted("STOP_HISTORY_PAGE_NOT_ALLOWED")
        elif params is not None:
            raise TriggerPilotHalted("GET_PARAMS_NOT_ALLOWED")
        return await self.__client._private_get(path, params)


async def _history(safe: _GetOnly, order_id: str, position_id: str) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    for page in range(1, MAX_PAGES + 1):
        part = await safe.get("/api/v1/private/stoporder/list/orders", {
            "symbol": SYMBOL, "is_finished": 1,
            "page_num": page, "page_size": 100,
        })
        if type(part) is not list or len(part) > 100 or any(type(x) is not dict for x in part):
            raise TriggerPilotHalted("HISTORY_SCHEMA_UNVERIFIED")
        pages += 1
        for row in part:
            sid = id_string(row.get("id"))
            if sid is None or sid in seen:
                raise TriggerPilotHalted("HISTORY_PAGINATION_DUPLICATE_OR_UNKEYED")
            seen.add(sid)
            if (
                row.get("symbol") == SYMBOL
                and id_string(row.get("orderId")) == order_id
                and id_string(row.get("positionId")) == position_id
            ):
                rows.append(row)
        if len(part) < 100:
            break
    return rows, pages


def _open_stop(stop: dict[str, Any], *, pid: str, oid: str, price: D) -> dict[str, Any]:
    qty_mode = stop.get("profitLossVolType")
    if qty_mode == "SEPARATE":
        qty = positive(stop.get("stopLossVol"))
    elif qty_mode == "SAME":
        qty = positive(stop.get("vol"))
        if qty is None and stop.get("volType") == 2:
            qty = D(1)
    else:
        qty = None
    active = (
        id_string(stop.get("id")) is not None
        and id_string(stop.get("positionId")) == pid
        and id_string(stop.get("orderId")) == oid
        and stop.get("symbol") == SYMBOL
        and stop.get("state") == 1
        and stop.get("isFinished") == 0
        and stop.get("triggerSide") == 0
        and stop.get("positionType") == 2
        and stop.get("lossTrend") == 1
        and positive(stop.get("stopLossPrice")) == price
        and qty == D(1)
        and (stop.get("stopLossType") is None
             or (type(stop.get("stopLossType")) is int
                 and stop["stopLossType"] == 0))
        and (stop.get("errorCode") is None
             or (type(stop.get("errorCode")) is int
                 and stop["errorCode"] == 0))
        and (stop.get("stopLossReverse") is None
             or (type(stop.get("stopLossReverse")) is int
                 and stop["stopLossReverse"] == 2))
    )
    return {
        "active_attached_sl_quantity_and_price_observed": bool(active),
        "stop_loss_type": (
            "MARKET" if type(stop.get("stopLossType")) is int
                and stop["stopLossType"] == 0
            else "LIMIT" if type(stop.get("stopLossType")) is int
                and stop["stopLossType"] == 1
            else "NOT_CERTIFIED"
        ),
        "quantity_mode": str(qty_mode) if qty_mode in ("SAME", "SEPARATE") else "UNVERIFIED",
        "error_code_zero": type(stop.get("errorCode")) is int and stop["errorCode"] == 0,
        "origin_is_order_attached": True,
        "sl_execution_verified": False,
    }


async def _status(client: Any, fingerprint: str) -> dict[str, Any]:
    body = _load(fingerprint)
    safe = _GetOnly(client)
    order = await safe.get(
        f"/api/v1/private/order/external/{SYMBOL}/{body['external_oid']}"
    )
    if type(order) is not dict or (
        id_string(order.get("orderId")) is None
        or order.get("externalOid") != body["external_oid"]
        or order.get("symbol") != SYMBOL
        or order.get("side") != 3
    ):
        raise TriggerPilotHalted("EXACT_ENTRY_ORDER_UNVERIFIED")
    order_id = id_string(order.get("orderId"))
    position_id = id_string(order.get("positionId"))
    if (
        body["entry_ack_verified"] is True
        and body.get("entry_order_id") != order_id
    ):
        raise TriggerPilotHalted("JOURNALED_ENTRY_ORDER_ID_MISMATCH")
    if position_id is None:
        raise TriggerPilotHalted("ENTRY_POSITION_ID_NOT_AVAILABLE")
    entry_filled = (
        order.get("state") == 3
        and order.get("openType") == 1
        and positive(order.get("dealVol")) == D(1)
        and positive(order.get("vol")) == D(1)
    )
    if not entry_filled:
        raise TriggerPilotHalted("EXACT_ENTRY_NOT_FILLED")
    safe_positions = await safe.get("/api/v1/private/position/open_positions")
    safe_stops = await safe.get("/api/v1/private/stoporder/open_orders")
    counts = await safe.get("/api/v1/private/order/open_order_total_count")
    if (
        type(safe_positions) is not list or type(safe_stops) is not list
        or type(counts) is not dict
        or any(type(x) is not dict for x in safe_positions + safe_stops)
        or any(type(counts.get(k)) is not int or counts[k] < 0 for k in (
            "sumCount", "limitOrderCount", "stopOrderCount",
            "planOrderCount", "trackOrderCount"
        ))
        or counts["stopOrderCount"] != len(safe_stops)
        or counts["sumCount"] != sum(counts[k] for k in (
            "limitOrderCount", "stopOrderCount",
            "planOrderCount", "trackOrderCount"
        ))
    ):
        raise TriggerPilotHalted("CURRENT_INVENTORY_OR_COUNT_UNVERIFIED")
    linked, pages = await _history(safe, order_id, position_id)
    if len(linked) > 1:
        raise TriggerPilotHalted("EMBEDDED_STOP_HISTORY_AMBIGUOUS")
    result: dict[str, Any] = {
        "status": "TRIGGER_RESULT_PENDING_OR_UNVERIFIED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "entry_filled": True,
        "journal_entry_acknowledged": body["entry_ack_verified"],
        "journal_manual_close_attempted": body["close_post_attempted"],
        "position_count": len(safe_positions),
        "stop_count": len(safe_stops),
        "open_order_count": counts["sumCount"],
        "linked_finished_stop_found": len(linked) == 1,
        "history_pages_read": pages,
        "finished_history_complete": False,
        "actual_sl_trigger_and_market_close_verified": False,
        "account_stably_flat_verified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent_by_status": 0,
    }
    if len(safe_positions) == 1:
        if any(counts[k] != 0 for k in (
            "limitOrderCount", "planOrderCount", "trackOrderCount"
        )):
            result["status"] = "UNRELATED_PENDING_ORDERS_CHECK_UI_NOW"
            return result
        p = safe_positions[0]
        if not (
            id_string(p.get("positionId")) == position_id
            and p.get("symbol") == SYMBOL
            and p.get("positionType") == 2
            and p.get("openType") == 1
            and positive(p.get("holdVol")) == D(1)
        ):
            result["status"] = "OPEN_POSITION_UNRELATED_OR_DRIFTED_CHECK_UI"
            return result
        if len(safe_stops) != 1:
            result["status"] = "SHORT_OPEN_WITHOUT_UNIQUE_SL_CHECK_UI"
            return result
        witness = _open_stop(
            safe_stops[0], pid=position_id, oid=order_id,
            price=D(body["stop_price"]),
        )
        result.update(witness)
        result["status"] = (
            "ONE_SHORT_WITH_ATTACHED_SL_FIELDS_OBSERVED_NONCERTIFYING"
            if witness["active_attached_sl_quantity_and_price_observed"]
            else "SHORT_OPEN_SL_UNVERIFIED_CHECK_UI_NOW"
        )
        return result
    if safe_positions or safe_stops or counts["sumCount"]:
        result["status"] = "ACCOUNT_NOT_FLAT_OR_RESIDUAL_STOP_CHECK_UI"
        return result

    # A flat snapshot is not proof of stop execution. Require bookended
    # full-account inventory and the exact MEXC-generated stop close order.
    before_flat = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(before_flat):
        result["status"] = "FLAT_ACCOUNT_BOOKEND_UNVERIFIED"
        return result
    result["account_stably_flat_verified"] = True
    if len(linked) != 1:
        result["status"] = "FLAT_BUT_STOP_HISTORY_NOT_FOUND"
        return result
    sl = linked[0]
    result["finished_stop_state"] = (
        "EXECUTED" if sl.get("state") == 3
        else "CANCELED" if sl.get("state") == 2
        else "FAILED" if sl.get("state") == 5
        else "OTHER_OR_UNVERIFIED"
    )
    result["finished_trigger_side"] = (
        "SL" if sl.get("triggerSide") == 2
        else "TP" if sl.get("triggerSide") == 1
        else "NOT_TRIGGERED" if sl.get("triggerSide") == 0
        else "UNVERIFIED"
    )
    if sl.get("state") != 3 or sl.get("triggerSide") != 2:
        result["status"] = "FLAT_NOT_VENUE_VERIFIED_SL_TRIGGER"
        return result
    close_id = id_string(sl.get("placeOrderId"))
    if (
        close_id is None
        or positive(sl.get("stopLossPrice")) != D(body["stop_price"])
        or sl.get("positionType") != 2
        or sl.get("isFinished") != 1
        or sl.get("lossTrend") != 1
        or type(sl.get("errorCode")) is not int
        or sl["errorCode"] != 0
        or (sl.get("stopLossType") is not None
            and (type(sl.get("stopLossType")) is not int
                 or sl["stopLossType"] != 0))
        or (sl.get("profitLossVolType") == "SEPARATE"
            and positive(sl.get("stopLossVol")) != D(1))
    ):
        result["status"] = "SL_TRIGGER_SEEN_LINKED_CLOSE_ID_OR_FIELDS_MISSING"
        return result
    executed_close = await safe.get(f"/api/v1/private/order/get/{close_id}")
    if not (
        type(executed_close) is dict
        and id_string(executed_close.get("orderId")) == close_id
        and id_string(executed_close.get("positionId")) == position_id
        and executed_close.get("symbol") == SYMBOL
        and executed_close.get("side") == 2
        and executed_close.get("state") == 3
        and executed_close.get("orderType") == 5
        and positive(executed_close.get("dealVol")) == D(1)
        and (executed_close.get("errorCode") is None
            or (type(executed_close.get("errorCode")) is int
                and executed_close["errorCode"] == 0))
    ):
        result["status"] = "SL_TRIGGER_SEEN_BUT_NATIVE_MARKET_CLOSE_FILL_UNVERIFIED"
        return result
    after_flat = await run_inventory(client=InventoryGetOnlyClient(client))
    if not _is_flat(after_flat):
        result["status"] = "SL_CLOSE_ACCOUNT_AFTER_BOOKEND_UNVERIFIED"
        result["account_stably_flat_verified"] = False
        return result
    result["actual_sl_trigger_and_market_close_verified"] = True
    result["status"] = "EXACT_SL_TRIGGER_AND_MARKET_CLOSE_FILL_VERIFIED_NONAUTHORIZING"
    return result


async def _close(client: Any, fingerprint: str) -> None:
    body = _load(fingerprint)
    if body["close_post_attempted"]:
        raise TriggerPilotHalted("CLOSE_ALREADY_POST_ATTEMPTED_NO_RETRY")
    safe = _GetOnly(client)
    order = await safe.get(f"/api/v1/private/order/external/{SYMBOL}/{body['external_oid']}")
    positions = await safe.get("/api/v1/private/position/open_positions")
    if type(order) is not dict or type(positions) is not list or len(positions) != 1:
        raise TriggerPilotHalted("EXACT_OWNED_SHORT_NOT_OPEN")
    p = positions[0]
    pid = id_string(order.get("positionId"))
    if not (
        type(p) is dict and pid is not None
        and id_string(p.get("positionId")) == pid
        and order.get("externalOid") == body["external_oid"]
        and order.get("symbol") == SYMBOL and order.get("side") == 3
        and order.get("state") == 3 and positive(order.get("dealVol")) == D(1)
        and (body["entry_ack_verified"] is not True
             or body.get("entry_order_id") == id_string(order.get("orderId")))
        and p.get("symbol") == SYMBOL
        and p.get("positionType") == 2 and p.get("openType") == 1
        and positive(p.get("holdVol")) == D(1)
    ):
        raise TriggerPilotHalted("CLOSE_POSITION_IDENTITY_NOT_MATCHED")
    _confirm("CLOSE TIGHT STOP PILOT SHORT")
    latest = await safe.get("/api/v1/private/position/open_positions")
    if (
        type(latest) is not list or len(latest) != 1
        or type(latest[0]) is not dict
        or any(str(latest[0].get(k)) != str(p.get(k)) for k in (
            "positionId", "symbol", "positionType", "openType",
            "leverage", "holdVol"
        ))
    ):
        raise TriggerPilotHalted("POSITION_CHANGED_BEFORE_MANUAL_CLOSE")
    close_oid = "TPWEB" + secrets.token_hex(12)
    payload = {
        "symbol": SYMBOL, "price": "0", "vol": 1,
        "side": 2, "type": 5, "openType": 1,
        "positionId": int(pid), "positionMode": 1,
        "externalOid": close_oid,
    }
    body["close_post_attempted"] = True
    body["close_oid"] = close_oid
    body["status"] = "CLOSE_POST_UNCERTAIN_NO_RETRY"
    _persist(body)
    print("CLOSE_POST_FENCED=true; ONE_REAL_CLOSE_ATTEMPT=true")
    try:
        ack = await client._private_post(OPEN_PATH, payload, throttle=False)
        close_id = id_string(ack.get("orderId")) if type(ack) is dict else None
        if close_id is None or close_id == body.get("entry_order_id"):
            raise TriggerPilotHalted("CLOSE_ACK_UNKNOWN_NO_RETRY")
        body["close_ack_verified"] = True
        body["status"] = "CLOSE_ACKED_FLAT_ACCOUNT_NOT_YET_VERIFIED"
        _persist(body)
    except BaseException:
        print("CLOSE_OUTCOME_UNCERTAIN_NO_RETRY_CHECK_MEXC_UI=true")
        raise
    print("CLOSE_ACK_VERIFIED=true; RUN_--status_TO_VERIFY_FLAT=true")


async def _watch(client: Any, fingerprint: str) -> None:
    print("WATCH_GET_ONLY=true; MAX_WATCH_SECONDS=240")
    for i in range(1, MAX_WATCH_SECONDS // 3 + 1):
        report = await _status(client, fingerprint)
        print("WATCH=" + json.dumps({
            "cycle": i, "status": report["status"],
            "position_count": report["position_count"],
            "stop_count": report["stop_count"],
            "sl_trigger_verified": report["actual_sl_trigger_and_market_close_verified"],
        }, sort_keys=True), flush=True)
        if report["actual_sl_trigger_and_market_close_verified"]:
            return
        if report["status"] not in (
            "ONE_SHORT_WITH_ATTACHED_SL_FIELDS_OBSERVED_NONCERTIFYING",
            "FLAT_BUT_STOP_HISTORY_NOT_FOUND",
        ):
            print("WATCH_STOPPED_INSPECT_MEXC_AND_USE_--close_IF_POSITION_OPEN=true")
            return
        if i < MAX_WATCH_SECONDS // 3:
            await asyncio.sleep(3)
    print("WATCH_EXPIRED_STOP_NOT_PROVEN; IF_SHORT_OPEN_USE_--close_OR_MEXC_UI=true")


async def _main(action: str) -> None:
    _allow_real_mode_env()
    if sys.platform != "darwin":
        raise TriggerPilotHalted("MAC_ONLY")
    key, secret = read_pilot_keychain_credentials(web_pilot=True)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient("https://api.mexc.com", api_key=key, api_secret=secret)
    try:
        if action == "check":
            report = await _preview(client)
            report["one_shot_journal_exists"] = JOURNAL.exists()
            print(json.dumps(report, sort_keys=True, indent=2))
            print("GET_ONLY_NO_ORDERS=true")
        elif action == "send":
            await _send(client, fingerprint)
        elif action == "status":
            print(json.dumps(await _status(client, fingerprint), sort_keys=True, indent=2))
            print("GET_ONLY_NO_ORDERS=true")
        elif action == "watch":
            await _watch(client, fingerprint)
        elif action == "close":
            await _close(client, fingerprint)
        else:
            raise TriggerPilotHalted("INVALID_ACTION")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator-only new T100 web subaccount tight SL-trigger experiment")
    mode = parser.add_mutually_exclusive_group(required=True)
    for label in ("check", "send", "status", "watch", "close"):
        mode.add_argument("--" + label, action="store_true")
    args = parser.parse_args()
    action = next(label for label in ("check", "send", "status", "watch", "close")
                  if getattr(args, label))
    try:
        with OneShotProcessLock():
            asyncio.run(_main(action))
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, TriggerPilotHalted) else type(exc).__name__
        print("PILOT_HALTED=" + reason)
        if action in ("send", "close"):
            print("DO_NOT_REPEAT_UNCERTAIN_POST_CHECK_MEXC_UI=true")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
