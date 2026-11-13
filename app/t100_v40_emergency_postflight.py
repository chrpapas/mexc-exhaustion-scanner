"""Reconcile V40 emergency market close without resubmitting any order.

A close ACK, missing position, or success HTTP response alone is never
evidence of a completed exit. Require exact externalOid+positionId order,
fully reconciled deal IDs/volumes/fees, and TWO independent full-account
and symbol-scoped signed GET bookends showing absolutely zero position,
native stop and pending orders. Only then may the separate journal advance
the original managed checkpoint to CLOSING -> CLOSED.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

from app.t100_live_fill_ledger import (
    VerifiedOrderFillSet,VerifiedFillLedgerHalted,
    build_verified_order_fill_set,
)
from app.t100_v40_emergency_close import (
    V40EmergencyCloseTarget,V40ExitFrame,_id,_num,
)


@dataclass(frozen=True)
class V40EmergencyClosePostflight:
    status:str
    exact_close_order_id:str|None=None
    exact_close_fill_set:VerifiedOrderFillSet|None=None
    exact_position_exit_verified:bool=False
    full_account_flat_double_get_verified:bool=False
    exact_close_fills_verified:bool=False
    journal_close_persisted:bool=False
    first_post_may_have_occurred:bool=True
    any_further_close_post_allowed:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False
    mexc_posts:int=0
    database_writes:int=0


def _flat(frame:V40ExitFrame)->bool:
    if (
        type(frame) is not V40ExitFrame
        or type(frame.positions) is not list or frame.positions
        or type(frame.symbol_positions) is not list or frame.symbol_positions
        or type(frame.stops) is not list or frame.stops
        or type(frame.symbol_stops) is not list or frame.symbol_stops
        or type(frame.hedge_mode) is not int or frame.hedge_mode!=1
        or type(frame.counts) is not dict
    ):
        return False
    required=(
        "sumCount","limitOrderCount","stopOrderCount",
        "planOrderCount","trackOrderCount",
    )
    return set(frame.counts)==set(required) and all(
        type(frame.counts[k]) is int and frame.counts[k]==0 for k in required
    )


def assess_emergency_close_postflight(
    *,target:V40EmergencyCloseTarget,close_order:Any,
    deals:Any,flat_first:V40ExitFrame,flat_second:V40ExitFrame,
)->V40EmergencyClosePostflight:
    block=lambda code:V40EmergencyClosePostflight(code)
    if (
        type(target) is not V40EmergencyCloseTarget
        or type(close_order) is not dict
        or type(deals) is not list
        or any(type(r) is not dict for r in deals)
    ):
        return block("V40_EMERGENCY_POSTFLIGHT_ORDER_OR_FILLS_MISSING")
    try:
        order_id=_id(close_order.get("orderId"))
        if (
            close_order.get("symbol")!=target.symbol
            or close_order.get("externalOid")!=target.close_external_oid
            or _id(close_order.get("positionId"))!=target.position_id
            or close_order.get("side")!=2
            or close_order.get("orderType")!=5
            or close_order.get("openType")!=2
            or close_order.get("state")!=3
            or close_order.get("errorCode")!=0
            or _num(close_order.get("dealVol"))!=target.quantity_contracts
            or close_order.get("feeCurrency")!="USDT"
        ):
            return block("V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT")
        fill_set=build_verified_order_fill_set(
            order_id=order_id,order=close_order,fills=deals,
            require_strategy_fully_filled=True,
        )
        if (
            type(fill_set) is not VerifiedOrderFillSet
            or fill_set.order_id!=order_id
            or fill_set.position_id!=target.position_id
            or fill_set.symbol!=target.symbol
            or fill_set.side!="CLOSE_SHORT"
            or fill_set.deal_volume!=target.quantity_contracts
            or fill_set.strategy_fully_filled_order_complete is not True
            or fill_set.final_order_fill_history_complete is not True
        ):
            return block("V40_EMERGENCY_POSTFLIGHT_EXACT_DEALS_NOT_VERIFIED")
        if not (_flat(flat_first) and _flat(flat_second)):
            return block("V40_EMERGENCY_POSTFLIGHT_ACCOUNT_NOT_FLAT")
    except Exception:
        return block("V40_EMERGENCY_POSTFLIGHT_EXACT_CLOSE_GET_UNVERIFIED")
    return V40EmergencyClosePostflight(
        "V40_EMERGENCY_CLOSE_ORDER_AND_FLAT_ACCOUNT_DOUBLE_GET_VERIFIED",
        exact_close_order_id=order_id,exact_close_fill_set=fill_set,
        exact_position_exit_verified=True,
        full_account_flat_double_get_verified=True,
        exact_close_fills_verified=True,
    )
