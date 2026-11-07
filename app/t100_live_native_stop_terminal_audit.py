"""Read-only MEXC terminal TP/SL audit. No exchange POST or live authorization.

2026-11-05 pilot: executed position-origin market-SL configured row linked to
filled market CLOSE_SHORT, but MEXC returned triggerSide=1 (TAKE_PROFIT).
Do not silently reinterpret this as verified stop-loss activation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class TerminalStopAudit:
    status: str
    venue_generated_close_filled: bool = False
    stop_loss_trigger_proven: bool = False
    venue_trigger_side_conflict: bool = False
    stop_loss_economic_behavior_consistent: bool = False
    account_flat_observed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _positive_id(value: Any) -> str | None:
    if type(value) not in (str, int):
        return None
    s = str(value)
    return s if s.isascii() and s.isdecimal() and any(c != "0" for c in s) else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or type(value) is bool:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def audit_terminal_native_short_stop(*, stop: Any, close_order: Any,
                                     expected_symbol: str,
                                     expected_position_id: str,
                                     expected_contracts: Decimal,
                                     expected_stop_price: Decimal,
                                     active_stop_previously_verified: bool,
                                     journal_fallback_post_absent: bool,
                                     account_stably_flat: bool,
                                     entry_order: Any = None,
                                     reviewed_stop_only_request: bool = False) -> TerminalStopAudit:
    """A linked filled close does NOT prove SL when triggerSide says TP."""
    def result(status: str, linked: bool = False, conflict: bool = False,
               economics: bool = False):
        return TerminalStopAudit(
            status=status, venue_generated_close_filled=linked,
            stop_loss_trigger_proven=status == "VERIFIED_NATIVE_STOP_LOSS_TRIGGER_FILLED",
            venue_trigger_side_conflict=conflict,
            stop_loss_economic_behavior_consistent=economics,
            account_flat_observed=account_stably_flat is True,
        )

    if (type(stop) is not dict or type(close_order) is not dict or
            type(expected_symbol) is not str or not expected_symbol or
            _positive_id(expected_position_id) is None or
            type(expected_contracts) is not Decimal or
            not expected_contracts.is_finite() or expected_contracts <= 0 or
            type(expected_stop_price) is not Decimal or
            not expected_stop_price.is_finite() or expected_stop_price <= 0 or
            type(active_stop_previously_verified) is not bool or
            type(journal_fallback_post_absent) is not bool or
            type(account_stably_flat) is not bool or
            type(reviewed_stop_only_request) is not bool):
        return TerminalStopAudit(status="TERMINAL_STOP_AUDIT_INPUT_INVALID")

    if not all((active_stop_previously_verified, journal_fallback_post_absent,
                account_stably_flat)):
        return result("TERMINAL_STOP_INDEPENDENCE_OR_FLATNESS_NOT_PROVEN")

    # Position-origin, full-volume, market SL setting, finished normal status.
    if (stop.get("symbol") != expected_symbol or
            str(stop.get("positionId")) != expected_position_id or
            _positive_id(stop.get("id")) is None or
            type(stop.get("orderId")) not in (int, str) or
            str(stop["orderId"]) != "0" or
            type(stop.get("state")) is not int or stop["state"] != 3 or
            type(stop.get("isFinished")) is not int or stop["isFinished"] != 1 or
            type(stop.get("positionType")) is not int or stop["positionType"] != 2 or
            type(stop.get("stopLossType")) is not int or stop["stopLossType"] != 0 or
            type(stop.get("volType")) is not int or stop["volType"] != 2 or
            type(stop.get("stopLossReverse")) is not int or stop["stopLossReverse"] != 2 or
            type(stop.get("lossTrend")) is not int or stop["lossTrend"] != 1 or
            type(stop.get("errorCode")) is not int or stop["errorCode"] != 0 or
            stop.get("profitLossVolType") != "SAME" or
            _decimal(stop.get("stopLossPrice")) != expected_stop_price or
            _decimal(stop.get("realityVol")) != expected_contracts):
        return result("TERMINAL_STOP_ROW_SEMANTICS_UNVERIFIED")

    placed_id = _positive_id(stop.get("placeOrderId"))
    if (placed_id is None or placed_id != _positive_id(close_order.get("orderId")) or
            close_order.get("symbol") != expected_symbol or
            str(close_order.get("positionId")) != expected_position_id or
            type(close_order.get("side")) is not int or close_order["side"] != 2 or
            type(close_order.get("state")) is not int or close_order["state"] != 3 or
            type(close_order.get("orderType")) is not int or close_order["orderType"] != 5 or
            type(close_order.get("errorCode")) is not int or close_order["errorCode"] != 0 or
            _decimal(close_order.get("dealVol")) != expected_contracts):
        return result("TERMINAL_STOP_LINKED_FILL_UNVERIFIED")

    # MEXC documented triggerSide: 1=take-profit, 2=stop-loss.
    side = stop.get("triggerSide")
    if type(side) is not int:
        return result("TERMINAL_TRIGGER_SIDE_INVALID", linked=True)
    if side == 1:
        # Extra forensic context can show the executed conditional close was
        # economically consistent with a SHORT stop loss, but must NEVER
        # override the venue's contradictory triggerSide=1 (take-profit).
        # No live authorization and no certified SL trigger follows from it.
        no_tp = stop.get("takeProfitPrice") is None
        entry_valid = (
            type(entry_order) is dict and
            entry_order.get("symbol") == expected_symbol and
            str(entry_order.get("positionId")) == expected_position_id and
            type(entry_order.get("side")) is int and entry_order["side"] == 3 and
            type(entry_order.get("state")) is int and entry_order["state"] == 3 and
            _decimal(entry_order.get("dealVol")) == expected_contracts
        )
        entry_avg = _decimal(entry_order.get("dealAvgPrice")) if entry_valid else None
        close_avg = _decimal(close_order.get("dealAvgPrice"))
        economics = bool(
            reviewed_stop_only_request and no_tp and entry_valid and
            entry_avg is not None and close_avg is not None and
            entry_avg > 0 and close_avg > entry_avg and
            expected_stop_price > entry_avg
        )
        if economics:
            return result("VENUE_TP_LABEL_CONFLICT_LOSS_DIRECTION_CLOSE",
                          linked=True, conflict=True, economics=True)
        return result("VENUE_TP_TRIGGER_SIDE_ON_MARKET_SL_CLOSE", linked=True, conflict=True)
    if side != 2:
        return result("TERMINAL_STOP_LOSS_TRIGGER_SIDE_UNVERIFIED", linked=True)
    return result("VERIFIED_NATIVE_STOP_LOSS_TRIGGER_FILLED", linked=True)
