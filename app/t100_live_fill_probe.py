"""Redacted, order-specific MEXC fill reconciliation.

Reads one immutable order and its deal-details via GET-only endpoints.
Returns only booleans/counts/enums and never emits symbols, order IDs,
trade IDs, prices, quantities, fees, or timestamps.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted


SIDE_LABELS = {
    1: "OPEN_LONG",
    2: "CLOSE_SHORT",
    3: "OPEN_SHORT",
    4: "CLOSE_LONG",
}
STATE_LABELS = {
    1: "PENDING",
    2: "UNFILLED",
    3: "FILLED",
    4: "CANCELED",
    5: "INVALID",
}


def _decimal(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _integer(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if str(value) == str(raw) else None


def assess_order_deal_reconciliation(
    order_id: str,
    order: Any,
    fills: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "fill_rows": 0,
        "order_side": "UNKNOWN",
        "order_state": "UNKNOWN",
        "all_fill_order_ids_match": False,
        "all_fill_symbols_match": False,
        "all_fill_sides_match": False,
        "fill_ids_unique": False,
        "fill_volume_matches_order_deal_volume": False,
        "weighted_fill_price_matches_order_average": False,
        "fee_currency_consistent": False,
        "fee_semantics_signed_expense_supported": True,
        "order_specific_fill_reconciliation_verified": False,
    }
    if (
        not isinstance(order_id, str) or not order_id.isdigit()
        or int(order_id) <= 0
        or not isinstance(order, dict)
        or not isinstance(fills, list)
        or any(not isinstance(row, dict) for row in fills)
    ):
        result["status"] = "INVALID_INPUT"
        return result

    oid = str(order.get("orderId") or "")
    symbol = order.get("symbol")
    side = _integer(order.get("side"))
    state = _integer(order.get("state"))
    deal_vol = _decimal(order.get("dealVol"))
    avg_price = _decimal(order.get("dealAvgPrice"))
    fee_currency = order.get("feeCurrency")

    result["order_side"] = SIDE_LABELS.get(side, "UNKNOWN")
    result["order_state"] = STATE_LABELS.get(state, "UNKNOWN")

    if (
        oid != order_id
        or not isinstance(symbol, str) or not symbol
        or side not in SIDE_LABELS
        or state not in STATE_LABELS
        or deal_vol is None or deal_vol < 0
        or (deal_vol > 0 and (avg_price is None or avg_price <= 0))
        or not isinstance(fee_currency, str) or not fee_currency
    ):
        result["status"] = "ORDER_CONTRACT_MISMATCH"
        return result

    result["fill_rows"] = len(fills)
    if deal_vol == 0:
        result["all_fill_order_ids_match"] = len(fills) == 0
        result["all_fill_symbols_match"] = len(fills) == 0
        result["all_fill_sides_match"] = len(fills) == 0
        result["fill_ids_unique"] = len(fills) == 0
        result["fill_volume_matches_order_deal_volume"] = len(fills) == 0
        result["weighted_fill_price_matches_order_average"] = len(fills) == 0
        result["fee_currency_consistent"] = len(fills) == 0
        result["order_specific_fill_reconciliation_verified"] = len(fills) == 0
        result["status"] = (
            "ZERO_FILL_ORDER_RECONCILED"
            if len(fills) == 0 else "ZERO_DEALVOL_WITH_FILL_ROWS"
        )
        return result

    if not fills:
        result["status"] = "DEALVOL_WITHOUT_FILL_ROWS"
        return result

    ids: set[str] = set()
    total_vol = Decimal(0)
    weighted_notional = Decimal(0)
    ids_match = symbols_match = sides_match = fee_ccy_ok = True

    for fill in fills:
        tid = str(fill.get("id") or "")
        f_oid = str(fill.get("orderId") or "")
        f_symbol = fill.get("symbol")
        f_side = _integer(fill.get("side"))
        vol = _decimal(fill.get("vol"))
        price = _decimal(fill.get("price"))
        fee = _decimal(fill.get("fee"))
        ccy = fill.get("feeCurrency")

        if (
            not tid or tid in ids
            or vol is None or vol <= 0
            or price is None or price <= 0
            or fee is None
        ):
            result["status"] = "MALFORMED_OR_DUPLICATE_FILL"
            return result
        ids.add(tid)

        if f_oid != order_id:
            ids_match = False
        if f_symbol != symbol:
            symbols_match = False
        if f_side != side:
            sides_match = False
        if ccy != fee_currency:
            fee_ccy_ok = False

        total_vol += vol
        weighted_notional += vol * price

    result["all_fill_order_ids_match"] = ids_match
    result["all_fill_symbols_match"] = symbols_match
    result["all_fill_sides_match"] = sides_match
    result["fill_ids_unique"] = len(ids) == len(fills)
    result["fee_currency_consistent"] = fee_ccy_ok
    result["fill_volume_matches_order_deal_volume"] = total_vol == deal_vol

    calculated_avg = weighted_notional / total_vol
    tolerance = max(Decimal("0.00000001"), abs(avg_price) * Decimal("0.00000001"))
    result["weighted_fill_price_matches_order_average"] = (
        abs(calculated_avg - avg_price) <= tolerance
    )

    result["order_specific_fill_reconciliation_verified"] = all((
        result["all_fill_order_ids_match"],
        result["all_fill_symbols_match"],
        result["all_fill_sides_match"],
        result["fill_ids_unique"],
        result["fill_volume_matches_order_deal_volume"],
        result["weighted_fill_price_matches_order_average"],
        result["fee_currency_consistent"],
    ))
    result["status"] = (
        "ORDER_SPECIFIC_FILLS_RECONCILED"
        if result["order_specific_fill_reconciliation_verified"]
        else "ORDER_SPECIFIC_FILL_MISMATCH"
    )
    return result


async def read_and_assess_order_fills(
    probe: ReadOnlyMexcProbe,
    *,
    order_id: str,
) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id.isdigit() or int(order_id) <= 0:
        raise VenueReadHalted("INVALID_ORDER_ID")
    order = await probe.raw_order_by_id(order_id)
    fills = await probe.raw_order_deal_details(order_id)
    return assess_order_deal_reconciliation(order_id, order, fills)
