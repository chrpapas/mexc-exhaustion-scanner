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

from app.t100_live_cross_risk_observation import (
    CrossRiskRead, _static_hash, _canonical,
)
from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS,
)
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
    # Only static, allowlisted FIELD NAMES. NEVER source values, IDs or symbols.
    changed_position_fields: tuple[str, ...] = ()
    changed_stop_fields: tuple[str, ...] = ()
    changed_asset_fields: tuple[str, ...] = ()
    private_risk_schedule_changed: bool = False
    public_contract_terms_changed: bool = False
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
    if (not value.is_finite() or (positive and value <= 0) or (not positive and not allow_negative and value < 0)):
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


def _critical_change_fields(
    first: CrossRiskRead, second: CrossRiskRead,
) -> dict[str, Any]:
    """Diagnose EXACT hashed categories, never emit raw venue values.

    Do not dismiss any mismatch. Current fields are drawn solely from the
    same pinned lists used by _static_hash; unknown raw keys are ignored.
    """
    def project(
        rows: list[dict[str, Any]], field: str,
        *, position: bool,
    ) -> tuple[str, ...]:
        if position:
            return tuple(sorted(_canonical({
                "positionId": row.get("positionId"),
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "value": row.get(field),
            }) for row in rows))
        return tuple(sorted(_canonical({
            "id": row.get("id"),
            "stopPlanOrderId": row.get("stopPlanOrderId"),
            "positionId": row.get("positionId"),
            "value": row.get(field),
        }) for row in rows))

    pos = tuple(
        key for key in CRITICAL_POSITION_FIELDS
        if project(first.positions, key, position=True)
        != project(second.positions, key, position=True)
    )
    stops = tuple(
        key for key in CRITICAL_STOP_FIELDS
        if project(first.stops, key, position=False)
        != project(second.stops, key, position=False)
    )
    asset_keys = (
        "currency", "cashBalance", "frozenBalance",
        "bonus", "debtAmount", "contributeMarginAmount",
    )
    assets = tuple(
        key for key in asset_keys
        if tuple(sorted(_canonical({
            "currency": row.get("currency"),
            "value": row.get(key),
        }) for row in first.assets))
        != tuple(sorted(_canonical({
            "currency": row.get("currency"),
            "value": row.get(key),
        }) for row in second.assets))
    )
    return {
        "changed_position_fields": pos,
        "changed_stop_fields": stops,
        "changed_asset_fields": assets,
        "private_risk_schedule_changed": (
            _canonical(first.private_risk_by_symbol)
            != _canonical(second.private_risk_by_symbol)
        ),
        "public_contract_terms_changed": (
            _canonical(first.public_contract_by_symbol)
            != _canonical(second.public_contract_by_symbol)
        ),
    }


async def observe_mixed_margin_account_window(
    reader: Any, *, now_ms: Callable[[], int],
    max_age_ms: int = 5000, max_separation_ms: int = 5000,
) -> MixedAccountObservation:
    def blocked(status: str, **details: Any) -> MixedAccountObservation:
        return MixedAccountObservation(status=status, **details)
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
        if not 0 < delta <= max_separation_ms or a[2] != b[2]:
            return blocked("MIXED_ACCOUNT_READ_WINDOW_OR_COUNTS_DRIFT")
        if a[0] != b[0]:
            # Maintain fail-closed status; add FIELD NAMES only. These
            # categories distinguish an economic cash movement from a
            # leverage/position/stop/contract-risk mutation.
            return blocked(
                "MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT",
                **_critical_change_fields(first, second),
            )
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
