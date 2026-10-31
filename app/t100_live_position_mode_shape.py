"""Pure, redacted account-wide MEXC futures position side/margin classifier.

This is an OBSERVATION, never ownership or a liquidation/risk certificate.
MEXC futures: positionType 1=LONG, 2=SHORT; openType 1=ISOLATED,
2=CROSS. Unknown codes, duplicate IDs, invalid volumes, and unknown
symbols fail closed rather than being guessed. Stops are intentionally
not required on manual positions.

Does not modify account state or call HTTP, Postgres, or any order endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
import re


@dataclass(frozen=True)
class PositionModeShape:
    status: str
    cross_short_count: int | None = None
    cross_long_count: int | None = None
    isolated_short_count: int | None = None
    isolated_long_count: int | None = None
    total_positions_observed: int | None = None
    observed_only: bool = True
    t100_position_ownership_verified: bool = False
    cross_collateral_certified: bool = False
    liquidation_risk_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def classify_account_position_modes(positions: Any) -> PositionModeShape:
    """Classify all observed rows, never silently omit manual positions."""
    blocked = PositionModeShape("POSITION_MODE_OR_IDENTITY_UNVERIFIED")
    if (
        type(positions) is not list
        or not 1 <= len(positions) <= 4
        or any(type(row) is not dict for row in positions)
    ):
        return blocked
    counts = {(2, 2): 0, (1, 2): 0, (2, 1): 0, (1, 1): 0}
    ids: set[str] = set()
    for row in positions:
        raw_id = row.get("positionId")
        symbol = row.get("symbol")
        side = row.get("positionType")
        mode = row.get("openType")
        try:
            qty = Decimal(str(row.get("holdVol")))
        except (InvalidOperation, ValueError, TypeError):
            return blocked
        if (
            isinstance(raw_id, bool) or raw_id is None
            or not str(raw_id).isdigit() or int(str(raw_id)) <= 0
            or str(raw_id) in ids
            or type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            or type(side) is not int or type(mode) is not int
            or (mode, side) not in counts
            or not qty.is_finite() or qty <= 0
        ):
            return blocked
        ids.add(str(raw_id))
        counts[mode, side] += 1
    return PositionModeShape(
        status=(
            "ACCOUNT_ALL_CROSS_SHORT_OBSERVED_NONAUTHORIZING"
            if counts[2, 2] == len(positions)
            else "ACCOUNT_MIXED_SIDE_OR_MARGIN_OBSERVED_NONAUTHORIZING"
        ),
        cross_short_count=counts[2, 2],
        cross_long_count=counts[2, 1],
        isolated_short_count=counts[1, 2],
        isolated_long_count=counts[1, 1],
        total_positions_observed=len(positions),
    )
