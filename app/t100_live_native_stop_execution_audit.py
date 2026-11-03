"""GET-only native-market-short-stop semantics audit for T100/V40.

MEXC's current TP/SL list documents stopLossType=0 as MARKET SL,
stopLossType=1 as LIMIT SL, stopLossReverse=2 as no reversal,
positionType=2 as SHORT and lossTrend=1 as latest-price trigger.
An active quantity-matched stop alone is not proof it will execute.

This report verifies fields of a supplied authenticated GET row ONLY:
- not endpoint completeness, current exchange position inventory, native
  order placement, fill reliability, gap losses or liquidation risk;
- never permits a POST, writes a database, stores venue identifiers,
  adopts user/manual positions or authorizes live trading.
Missing optional/undocumented venue fields are conservative blockers.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_active_tpsl import effective_protective_quantity


def _positive(raw: Any) -> Decimal | None:
    if raw is None or type(raw) is bool:
        return None
    try:
        n = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return n if n.is_finite() and n > 0 else None


def _positive_id(raw: Any) -> bool:
    if type(raw) not in (int, str):
        return False
    value = str(raw)
    # Unicode isdigit() accepts some glyphs int() cannot parse. Treat
    # external venue IDs as ASCII digits and never raise for malformed GETs.
    return value.isascii() and value.isdigit() and any(ch != "0" for ch in value)


@dataclass(frozen=True)
class NativeMarketShortStopAudit:
    status: str
    row_shape_verified: bool = False
    owned_position_match_verified: bool = False
    active_native_full_volume_market_sl_observed: bool = False
    stop_execution_certified: bool = False
    exchange_atomicity_certified: bool = False
    account_wide_completeness_certified: bool = False
    cross_margin_liquidation_certified: bool = False
    manual_position_adopted: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def assess_native_short_market_stop_row(
    *,
    stop: Any,
    symbol: str,
    position_id: str,
    position_quantity: Decimal,
    maximum_short_stop_price: Decimal,
) -> NativeMarketShortStopAudit:
    """Strict static current-GET field witness, not execution certification."""
    def fail(code: str) -> NativeMarketShortStopAudit:
        return NativeMarketShortStopAudit(status=code)

    if (
        type(stop) is not dict
        or type(symbol) is not str or not symbol
        or type(position_id) is not str or not _positive_id(position_id)
        or type(position_quantity) is not Decimal
        or _positive(position_quantity) is None
        or type(maximum_short_stop_price) is not Decimal
        or _positive(maximum_short_stop_price) is None
    ):
        return fail("NATIVE_SL_AUDIT_INPUT_INVALID")

    if (
        not _positive_id(stop.get("id"))
        or not _positive_id(stop.get("positionId"))
        or str(stop["positionId"]) != position_id
        or stop.get("symbol") != symbol
    ):
        return fail("NATIVE_SL_IDENTITY_OR_OWNERSHIP_UNVERIFIED")

    verified = dict(
        row_shape_verified=True,
        owned_position_match_verified=True,
    )
    if (
        type(stop.get("state")) is not int or stop["state"] != 1
        or type(stop.get("isFinished")) is not int
        or stop["isFinished"] != 0
        or type(stop.get("positionType")) is not int
        or stop["positionType"] != 2
    ):
        return NativeMarketShortStopAudit(
            status="NATIVE_SL_INACTIVE_OR_WRONG_SIDE", **verified,
        )

    # The MEXC documentation differentiates stop-market from stop-limit.
    # A limit SL may remain unfilled after a gap; missing type must NOT
    # silently inherit market-stop semantics.
    if (
        type(stop.get("stopLossType")) is not int
        or stop["stopLossType"] != 0
        or type(stop.get("stopLossReverse")) is not int
        or stop["stopLossReverse"] != 2
        or type(stop.get("lossTrend")) is not int
        or stop["lossTrend"] != 1
    ):
        return NativeMarketShortStopAudit(
            status="NATIVE_SL_MARKET_TRIGGER_OR_REVERSE_UNVERIFIED",
            **verified,
        )
    actual_stop = _positive(stop.get("stopLossPrice"))
    if actual_stop is None or actual_stop > maximum_short_stop_price:
        return NativeMarketShortStopAudit(
            status="NATIVE_SL_PRICE_NOT_PROTECTIVE", **verified,
        )

    # The shared legacy volume interpreter predates explicit MEXC mode
    # fields. This stricter native-market witness must reject unknown or
    # absent modes before using it to assess full-position coverage.
    if (
        type(stop.get("volType")) is not int
        or stop["volType"] not in (1, 2)
        or stop.get("profitLossVolType") not in ("SAME", "SEPARATE")
    ):
        return NativeMarketShortStopAudit(
            status="NATIVE_SL_FULL_CONTRACT_COVERAGE_UNVERIFIED",
            **verified,
        )

    effective = effective_protective_quantity(
        stop, position_quantity=position_quantity,
    )
    if effective != position_quantity:
        return NativeMarketShortStopAudit(
            status="NATIVE_SL_FULL_CONTRACT_COVERAGE_UNVERIFIED",
            **verified,
        )

    return NativeMarketShortStopAudit(
        status="NATIVE_MARKET_SHORT_SL_ROW_OBSERVED_NONAUTHORIZING",
        active_native_full_volume_market_sl_observed=True,
        **verified,
    )
