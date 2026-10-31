"""Strict but nonauthorizing mixed-margin account GET-only observation.

Unlike the legacy all-CROSS-SHORT risk tier witness, this verifies current
account-wide *economic and identity parity* for a mix of cross/isolated and
long/short USDT positions. It NEVER treats isolated unrealized profits as
cross-available collateral and NEVER certifies liquidation, future tiers,
SL coverage, account ownership, or permission to trade.

Inject an already verified full-account GET-only reader. No POST, no DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.t100_live_cross_risk_observation import CrossRiskRead, _static_hash
from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_position_mode_shape import classify_account_position_modes


@dataclass(frozen=True)
class MixedAccountObservation:
    status: str
    positions_observed: int | None = None
    cross_margin_positions: int | None = None
    isolated_margin_positions: int | None = None
    economic_parity_observed: bool = False
    stable_critical_inventory: bool = False
    fair_price_window_observed: bool = False
    isolated_margin_reserved_as_separate_observation: bool = False
    isolated_unrealized_counted_as_cross_collateral: bool = False
    cross_account_collateral_certified: bool = False
    current_risk_tiers_certified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _finite(
    raw: Any, *, positive: bool = False, allow_negative: bool = False,
) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise ValueError("INVALID_ECONOMIC_VALUE")
    try:
        value = Decimal(str(raw))
    except (TypeError, InvalidOperation, ValueError):
        raise ValueError("INVALID_ECONOMIC_VALUE") from None
    if not value.is_finite() or (value <= 0 if positive else value < 0 if not allow_negative else False):
        raise ValueError("INVALID_ECONOMIC_VALUE")
    return value


def _inspect_frame(
    frame: Any, *, now: int, max_age_ms: int,
) -> tuple[str, dict[str, Decimal], int, int, int]:
    if type(frame) is not CrossRiskRead or type(now) is not int:
        raise ValueError("INVALID_MIXED_FRAME")
    if (
        type(frame.observed_at_ms) is not int
        or frame.observed_at_ms > now + 100
        or now - frame.observed_at_ms > max_age_ms
        or not all(getattr(frame, field, None) is True for field in (
            "positions_complete", "stops_complete", "assets_complete"
        ))
        or type(frame.stops) is not list
        or any(type(row) is not dict for row in frame.stops)
        or type(frame.assets) is not list
        or any(type(row) is not dict for row in frame.assets)
        or type(frame.positions) is not list
    ):
        raise ValueError("INVALID_MIXED_FRAME")

    shape = classify_account_position_modes(frame.positions)
    if shape.status != "ACCOUNT_MIXED_SIDE_OR_MARGIN_OBSERVED_NONAUTHORIZING":
        raise ValueError("MIXED_MODE_NOT_OBSERVED_OR_INVALID")
    economics = assess_position_margin_witness(frame.positions, frame.assets)
    if economics.get("position_inventory_economic_witness_observed") is not True:
        raise ValueError("MIXED_ECONOMIC_PARITY_UNVERIFIED")
    usdt = [r for r in frame.assets if r.get("currency") == "USDT"]
    if len(usdt) != 1:
        raise ValueError("USDT_ASSET_NOT_UNIQUE")
    for field in ("cashBalance", "availableBalance", "equity"):
        _finite(usdt[0].get(field), positive=field == "equity")

    symbols = {row["symbol"] for row in frame.positions}
    if (
        len(symbols) != len(frame.positions)
        or set(frame.private_risk_by_symbol) != symbols
        or set(frame.public_contract_by_symbol) != symbols
        or set(frame.fair_by_symbol) != symbols
    ):
        raise ValueError("MIXED_SYMBOL_SCOPES_UNVERIFIED")
    marks: dict[str, Decimal] = {}
    for pos in frame.positions:
        _finite(pos.get("holdVol"), positive=True)
        _finite(pos.get("im"))
        _finite(pos.get("unRealizedPnl"), allow_negative=True)
        _finite(pos.get("holdAvgPrice"), positive=True)
        fair = frame.fair_by_symbol[pos["symbol"]]
        if (
            type(fair) is not dict
            or type(fair.get("observed_at_ms")) is not int
            or abs(fair["observed_at_ms"] - frame.observed_at_ms) > max_age_ms
        ):
            raise ValueError("MIXED_FAIR_TIMESTAMP_UNVERIFIED")
        marks[pos["symbol"]] = _finite(fair.get("fairPrice"), positive=True)
    # The isolated im is part of the account-wide positionMargin witness,
    # but cross-wallet collateral is NOT inferred from it or from cashBalance.
    # A separate, independently verified venue collateral model is required.
    return (
        _static_hash(frame), marks, len(frame.positions),
        shape.cross_short_count + shape.cross_long_count,
        shape.isolated_short_count + shape.isolated_long_count,
    )


async def observe_mixed_margin_account_window(
    reader: Any, *, now_ms: Callable[[], int],
    max_age_ms: int = 5000, max_separation_ms: int = 5000,
) -> MixedAccountObservation:
    def blocked(status: str) -> MixedAccountObservation:
        return MixedAccountObservation(status=status)
    if (
        not callable(getattr(reader, "read", None))
        or not callable(now_ms)
        or type(max_age_ms) is not int or not 0 < max_age_ms <= 5000
        or type(max_separation_ms) is not int
        or not 0 < max_separation_ms <= 5000
    ):
        return blocked("MIXED_ACCOUNT_INPUT_INVALID")
    try:
        first = await reader.read()
        a = _inspect_frame(first, now=now_ms(), max_age_ms=max_age_ms)
        second = await reader.read()
        b = _inspect_frame(second, now=now_ms(), max_age_ms=max_age_ms)
        delta = second.observed_at_ms - first.observed_at_ms
        if not 0 < delta <= max_separation_ms or a[2:] != b[2:]:
            return blocked("MIXED_ACCOUNT_READ_WINDOW_OR_COUNTS_DRIFT")
        if a[0] != b[0]:
            return blocked("MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT")
        if set(a[1]) != set(b[1]) or any(
            abs(b[1][sym] / a[1][sym] - Decimal(1)) > Decimal("0.01")
            for sym in a[1]
        ):
            return blocked("MIXED_ACCOUNT_FAIR_PRICE_DRIFT")
        return MixedAccountObservation(
            status="MIXED_ACCOUNT_ECONOMIC_BOOKENDS_OBSERVED_NONAUTHORIZING",
            positions_observed=b[2],
            cross_margin_positions=b[3],
            isolated_margin_positions=b[4],
            economic_parity_observed=True,
            stable_critical_inventory=True,
            fair_price_window_observed=True,
            isolated_margin_reserved_as_separate_observation=b[4] > 0,
        )
    except Exception:
        return blocked("MIXED_ACCOUNT_READ_OR_SCHEMA_UNVERIFIED")
