"""Redacted economic witness for observed MEXC position inventory.

Compares account-level positionMargin/unrealized against sums from observed
USDT position rows. This is independent consistency evidence only; it does
not by itself prove endpoint completeness or exchange atomicity.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


class PositionMarginWitnessHalted(RuntimeError):
    pass


def _decimal(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PositionMarginWitnessHalted(f"INVALID_{label}") from exc
    if not value.is_finite():
        raise PositionMarginWitnessHalted(f"INVALID_{label}")
    return value


def _close(a: Decimal, b: Decimal) -> bool:
    tolerance = max(
        Decimal("0.00000001"),
        max(abs(a), abs(b)) * Decimal("0.00000001"),
    )
    return abs(a - b) <= tolerance


def assess_position_margin_witness(
    positions: Any,
    assets: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "observed_positions": 0,
        "returned_asset_rows": 0,
        "usdt_asset_row_unique": False,
        "all_observed_positions_usdt_settled": False,
        "usdt_position_margin_reconciled": False,
        "usdt_unrealized_reconciled": False,
        "returned_non_usdt_position_margin_zero": False,
        "returned_non_usdt_unrealized_zero": False,
        "position_inventory_margin_witness_observed": False,
        "position_inventory_economic_witness_observed": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
    }
    if (
        not isinstance(positions, list)
        or not isinstance(assets, list)
        or any(not isinstance(row, dict) for row in positions)
        or any(not isinstance(row, dict) for row in assets)
    ):
        result["status"] = "ECONOMIC_WITNESS_INPUT_AMBIGUOUS"
        return result

    result["observed_positions"] = len(positions)
    result["returned_asset_rows"] = len(assets)

    usdt_rows = [
        row for row in assets
        if row.get("currency") == "USDT"
    ]
    if len(usdt_rows) != 1:
        result["status"] = "USDT_ASSET_ROW_NOT_UNIQUE"
        return result
    result["usdt_asset_row_unique"] = True

    if any(
        not isinstance(row.get("symbol"), str)
        or not row["symbol"].endswith("_USDT")
        for row in positions
    ):
        result["status"] = "NON_USDT_POSITION_OBSERVED"
        return result
    result["all_observed_positions_usdt_settled"] = True

    try:
        observed_margin = sum(
            (_decimal(row.get("im"), "POSITION_IM") for row in positions),
            Decimal(0),
        )
        observed_unrealized = sum(
            (
                _decimal(
                    row.get("unRealizedPnl"),
                    "POSITION_UNREALIZED_PNL",
                )
                for row in positions
            ),
            Decimal(0),
        )
        usdt_margin = _decimal(
            usdt_rows[0].get("positionMargin"),
            "USDT_POSITION_MARGIN",
        )
        usdt_unrealized = _decimal(
            usdt_rows[0].get("unrealized"),
            "USDT_UNREALIZED",
        )

        other_assets = [
            row for row in assets
            if row.get("currency") != "USDT"
        ]
        other_margins = [
            _decimal(
                row.get("positionMargin"),
                "NON_USDT_POSITION_MARGIN",
            )
            for row in other_assets
        ]
        other_unrealized = [
            _decimal(
                row.get("unrealized"),
                "NON_USDT_UNREALIZED",
            )
            for row in other_assets
        ]
    except PositionMarginWitnessHalted as exc:
        result["status"] = str(exc)
        return result

    margin_match = _close(observed_margin, usdt_margin)
    unrealized_match = _close(observed_unrealized, usdt_unrealized)
    non_usdt_margin_zero = all(x == 0 for x in other_margins)
    non_usdt_unrealized_zero = all(x == 0 for x in other_unrealized)

    result["usdt_position_margin_reconciled"] = margin_match
    result["usdt_unrealized_reconciled"] = unrealized_match
    result["returned_non_usdt_position_margin_zero"] = (
        non_usdt_margin_zero
    )
    result["returned_non_usdt_unrealized_zero"] = (
        non_usdt_unrealized_zero
    )
    result["position_inventory_margin_witness_observed"] = all((
        margin_match,
        non_usdt_margin_zero,
    ))
    result["position_inventory_economic_witness_observed"] = all((
        result["position_inventory_margin_witness_observed"],
        unrealized_match,
        non_usdt_unrealized_zero,
    ))
    if result["position_inventory_economic_witness_observed"]:
        result["status"] = "POSITION_INVENTORY_ECONOMIC_WITNESS_MATCH"
    elif result["position_inventory_margin_witness_observed"]:
        result["status"] = (
            "POSITION_MARGIN_WITNESS_MATCH_UNREALIZED_NOT_RECONCILED"
        )
    else:
        result["status"] = "POSITION_INVENTORY_ECONOMIC_WITNESS_MISMATCH"
    return result
