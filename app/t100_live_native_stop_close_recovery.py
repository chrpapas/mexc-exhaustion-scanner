"""T100 native-stop closure recovery (signed GETs ONLY).

For a previously enrolled T100-owned short, independently reconcile:
  exact originating OPEN_SHORT filled order;
  exact, position-origin finished native market stop;
  exchange-generated CLOSE_SHORT market order (placeOrderId); and
  positively matched terminal MEXC historical position (bare arrays allowed).

The real V5 pilot returned triggerSide=1 (documented TAKE_PROFIT) even with
stopLossPrice configured and no TP price. NEVER override that conflict,
claim that the SL side was proven, or authorize another order.

Other/manual positions may remain open. This certifies ONLY the exact
owned position, not an account-wide flat or complete venue history.
No POST, DB write, stop amendment, retry, adoption, or live authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, _validate_owned, _amount, _valid_id,
)
from app.t100_live_owned_position_finality import (
    verify_owned_position_finality, OwnedPositionFinalityHalted,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


@dataclass(frozen=True)
class NativeStopRecoveryReport:
    status: str
    entry_filled_verified: bool = False
    native_stop_terminal_verified: bool = False
    linked_venue_close_filled: bool = False
    owned_position_closed_verified: bool = False
    venue_trigger_side_conflict: bool = False
    stop_loss_trigger_side_proven: bool = False
    historical_pages_exhausted: bool = False
    manual_position_management_allowed: bool = False
    whole_account_flat_certified: bool = False
    entry_allowed: bool = False
    emergency_close_allowed: bool = False
    live_authorization: bool = False


def _safe_decimal(value: Any) -> Decimal | None:
    if value is None or type(value) is bool:
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _valid_entry(row: Any, owned: OwnedShort) -> bool:
    return bool(
        type(row) is dict and
        str(row.get("orderId")) == owned.entry_order_id and
        row.get("externalOid") == owned.entry_external_oid and
        row.get("symbol") == owned.symbol and
        str(row.get("positionId")) == owned.position_id and
        type(row.get("side")) is int and row["side"] == 3 and
        type(row.get("state")) is int and row["state"] == 3 and
        _amount(row.get("dealVol")) == owned.contracts
    )


def _exact_terminal_stop(row: Any, owned: OwnedShort) -> bool:
    return bool(
        type(row) is dict and
        str(row.get("id") or row.get("stopPlanOrderId")) == owned.stop_order_id and
        row.get("symbol") == owned.symbol and
        str(row.get("positionId")) == owned.position_id and
        type(row.get("orderId")) in (str, int) and str(row["orderId"]) == "0" and
        type(row.get("positionType")) is int and row["positionType"] == 2 and
        type(row.get("state")) is int and row["state"] == 3 and
        type(row.get("isFinished")) is int and row["isFinished"] == 1 and
        type(row.get("stopLossType")) is int and row["stopLossType"] == 0 and
        type(row.get("volType")) is int and row["volType"] == 2 and
        type(row.get("stopLossReverse")) is int and row["stopLossReverse"] == 2 and
        type(row.get("lossTrend")) is int and row["lossTrend"] == 1 and
        type(row.get("errorCode")) is int and row["errorCode"] == 0 and
        row.get("profitLossVolType") == "SAME" and
        _safe_decimal(row.get("stopLossPrice")) == owned.stop_price and
        _safe_decimal(row.get("realityVol")) == owned.contracts and
        row.get("takeProfitPrice") is None and
        type(row.get("triggerSide")) is int and row["triggerSide"] in (1, 2) and
        _valid_id(row.get("placeOrderId"))
    )


def _same_stop_critical(a: dict[str, Any], b: dict[str, Any]) -> bool:
    fields = (
        "id", "stopPlanOrderId", "positionId", "symbol", "orderId",
        "positionType", "state", "isFinished", "stopLossType",
        "volType", "stopLossReverse", "lossTrend", "errorCode",
        "profitLossVolType", "stopLossPrice", "realityVol",
        "takeProfitPrice", "triggerSide", "placeOrderId",
    )
    return all(a.get(k) == b.get(k) for k in fields)


def _exact_stop_on_page(rows: Any, owned: OwnedShort) -> dict[str, Any] | None:
    if type(rows) is not list or any(type(x) is not dict for x in rows):
        return None
    candidates = [
        row for row in rows
        if str(row.get("id") or row.get("stopPlanOrderId")) == owned.stop_order_id
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _valid_close(row: Any, owned: OwnedShort, close_id: str) -> bool:
    return bool(
        type(row) is dict and
        str(row.get("orderId")) == close_id and
        row.get("symbol") == owned.symbol and
        str(row.get("positionId")) == owned.position_id and
        type(row.get("side")) is int and row["side"] == 2 and
        type(row.get("state")) is int and row["state"] == 3 and
        type(row.get("orderType")) is int and row["orderType"] == 5 and
        type(row.get("errorCode")) is int and row["errorCode"] == 0 and
        _amount(row.get("dealVol")) == owned.contracts
    )


async def recover_native_stop_close_read_only(
    *, owned: OwnedShort, actual_scope: str, probe: ReadOnlyMexcProbe,
) -> NativeStopRecoveryReport:
    """All-or-nothing positive close certification, GET-only, fail closed.

    The caller must provide an independently account-enrolled OwnedShort.
    This function does not attest a caller-supplied account fingerprint.
    """
    _validate_owned(owned, actual_scope)
    if not isinstance(probe, ReadOnlyMexcProbe):
        return NativeStopRecoveryReport("STRICT_SIGNED_GET_PROBE_REQUIRED")

    try:
        source = await probe.raw_order_by_external_oid(
            owned.symbol, owned.entry_external_oid)
        if not _valid_entry(source, owned):
            return NativeStopRecoveryReport("OWNED_ENTRY_ORIGIN_UNVERIFIED")
        entry = await probe.raw_order_by_id(owned.entry_order_id)
        if not _valid_entry(entry, owned):
            return NativeStopRecoveryReport("OWNED_ENTRY_FILL_UNVERIFIED")
    except Exception:
        return NativeStopRecoveryReport("OWNED_ENTRY_SIGNED_GET_UNVERIFIED")
    initial = dict(entry_filled_verified=True)

    # Stop lists have no independently attested total. A positive exact
    # match is useful; absence on a page says nothing about finality.
    try:
        a = _exact_stop_on_page(
            await probe.raw_stop_page(1, 100, is_finished=1), owned)
        b = _exact_stop_on_page(
            await probe.raw_stop_page(1, 100, is_finished=1), owned)
    except Exception:
        return NativeStopRecoveryReport("STOP_HISTORY_GET_UNVERIFIED",**initial)
    if a is None or b is None:
        return NativeStopRecoveryReport(
            "EXACT_FINISHED_STOP_NOT_OBSERVED_ON_BOTH_PAGES",**initial)
    if not _same_stop_critical(a,b):
        return NativeStopRecoveryReport(
            "FINISHED_NATIVE_STOP_RECORD_CHANGED",**initial)
    if not _exact_terminal_stop(a,owned):
        return NativeStopRecoveryReport(
            "FINISHED_NATIVE_STOP_SEMANTICS_UNVERIFIED",**initial)

    state = dict(initial,native_stop_terminal_verified=True)
    close_id=str(a["placeOrderId"])
    try:
        close = await probe.raw_order_by_id(close_id)
    except Exception:
        return NativeStopRecoveryReport("NATIVE_STOP_CLOSE_GET_UNVERIFIED",**state)
    if not _valid_close(close,owned,close_id):
        return NativeStopRecoveryReport(
            "NATIVE_STOP_GENERATED_CLOSE_FILL_UNVERIFIED",**state)
    state["linked_venue_close_filled"]=True

    side=a["triggerSide"]
    state["venue_trigger_side_conflict"]=side == 1
    state["stop_loss_trigger_side_proven"]=side == 2

    # A full market CLOSE_SHORT and a terminal stop record still do not
    # alone prove that the original owned position finished and stayed
    # closed. Require independent exact positive historical record.
    try:
        finality = await verify_owned_position_finality(
            owned=owned,actual_scope=actual_scope,probe=probe)
    except OwnedPositionFinalityHalted:
        return NativeStopRecoveryReport(
            "LINKED_CLOSE_FILLED_POSITION_HISTORY_GET_UNVERIFIED",**state)
    if not finality.closed_position_verified:
        return NativeStopRecoveryReport(
            "LINKED_CLOSE_FILLED_OWNED_CLOSURE_NOT_PROVEN",**state)

    state.update(
        owned_position_closed_verified=True,
        historical_pages_exhausted=finality.historical_pages_exhausted,
    )
    return NativeStopRecoveryReport(
        status=("NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_VENUE_TP_LABEL_CONFLICT"
                if side == 1 else
                "NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_SL_LABEL"),
        **state,
    )
