"""Bounded GET-only PnL cross-endpoint synchronization diagnostic.

Three sequential samples, each positions -> account assets -> positions.
No post, no retries, no orders, no credentials, no database/network clients.
The two venue endpoints are not atomic. A stable sample may reveal persistent
disagreement, but matching individual samples NEVER certifies collateral,
exchange atomicity, liquidation economics, or trading authorization.
Only summary booleans/counts are returned; never symbols, amounts, prices,
position IDs, exchange errors or account identifiers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS
from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


@dataclass(frozen=True)
class PnlSyncReport:
    status: str
    sampled_windows: int = 0
    position_pnl_moved_windows: int = 0
    stable_position_pnl_windows: int = 0
    stable_unrealized_disagreement_windows: int = 0
    stable_unrealized_reconciled_windows: int = 0
    margin_mismatch_windows: int = 0
    position_inventory_drift_windows: int = 0
    observed_non_usdt_exposure_windows: int = 0
    account_wide_completeness_certified: bool = False
    cross_wallet_collateral_certified: bool = False
    exchange_atomicity_verified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _strict_decimal(value: Any) -> Decimal:
    if value is None or type(value) is bool:
        raise ValueError("INVALID_PNL_SCALAR")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("INVALID_PNL_SCALAR") from None
    if not d.is_finite():
        raise ValueError("INVALID_PNL_SCALAR")
    return d


def _positions_critical(rows: Any) -> tuple[str, ...]:
    if (
        type(rows) is not list or not 1 <= len(rows) <= 4
        or any(type(r) is not dict for r in rows)
    ):
        raise ValueError("POSITION_INVENTORY_UNVERIFIED")
    return tuple(sorted(json.dumps(
        {k: r[k] for k in CRITICAL_POSITION_FIELDS if k in r},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) for r in rows))


def _pnl_sum(rows: list[dict[str, Any]]) -> Decimal:
    return sum((_strict_decimal(p.get("unRealizedPnl")) for p in rows), Decimal(0))


def _one_window(positions_before: Any, assets: Any, positions_after: Any) -> dict[str, bool]:
    if _positions_critical(positions_before) != _positions_critical(positions_after):
        return {"position_inventory_drift": True}
    # Compare by immutable position identity, not only SUM (offsetting
    # PnL moves in different contracts must not masquerade as stability).
    pnl_before = {
        str(p.get("positionId")): _strict_decimal(p.get("unRealizedPnl"))
        for p in positions_before
    }
    pnl_after = {
        str(p.get("positionId")): _strict_decimal(p.get("unRealizedPnl"))
        for p in positions_after
    }
    if len(pnl_before) != len(positions_before) or len(pnl_after) != len(positions_after):
        raise ValueError("DUPLICATE_POSITION_IDENTITY")
    pnl_moved = pnl_before != pnl_after
    before = assess_position_margin_witness(positions_before, assets)
    after = assess_position_margin_witness(positions_after, assets)
    allowed_statuses = {
        "POSITION_INVENTORY_ECONOMIC_WITNESS_MATCH",
        "POSITION_MARGIN_WITNESS_MATCH_UNREALIZED_NOT_RECONCILED",
        "POSITION_INVENTORY_ECONOMIC_WITNESS_MISMATCH",
    }
    if before.get("status") not in allowed_statuses or after.get("status") not in allowed_statuses:
        raise ValueError("POSITION_ASSETS_SCHEMA_UNVERIFIED")
    if before.get("usdt_asset_row_unique") is not True or (
        before.get("all_observed_positions_usdt_settled") is not True
    ):
        raise ValueError("POSITION_ASSETS_SCOPE_UNVERIFIED")
    if any(
        type(x.get(flag)) is not bool for x in (before, after)
        for flag in (
            "usdt_position_margin_reconciled", "usdt_unrealized_reconciled",
            "returned_non_usdt_position_margin_zero",
            "returned_non_usdt_unrealized_zero",
        )
    ):
        raise ValueError("POSITION_ASSETS_SCHEMA_UNVERIFIED")
    return {
        "position_inventory_drift": False,
        "position_pnl_moved": pnl_moved,
        "stable_unrealized_disagreement": (
            not pnl_moved and
            before["usdt_unrealized_reconciled"] is False
        ),
        "stable_unrealized_reconciled": (
            not pnl_moved and
            before["usdt_unrealized_reconciled"] is True
        ),
        "margin_mismatch": (
            before["usdt_position_margin_reconciled"] is False or
            after["usdt_position_margin_reconciled"] is False
        ),
        "non_usdt_exposure": (
            before["returned_non_usdt_position_margin_zero"] is False or
            before["returned_non_usdt_unrealized_zero"] is False
        ),
    }


async def observe_pnl_sync(
    private: ReadOnlyMexcProbe, *, monotonic_ms: Callable[[], int],
    samples: int = 3, max_window_ms: int = 2500,
) -> PnlSyncReport:
    """No retry after any failed read; three capped read triplets by default."""
    def failure(status: str) -> PnlSyncReport:
        return PnlSyncReport(status=status)

    if (
        type(private) is not ReadOnlyMexcProbe
        or not callable(monotonic_ms)
        or type(samples) is not int or not 1 <= samples <= 3
        or type(max_window_ms) is not int or not 0 < max_window_ms <= 5000
    ):
        return failure("PNL_SYNC_INVALID_CONFIG")
    observations: list[dict[str, bool]] = []
    try:
        for _ in range(samples):
            start = monotonic_ms()
            before = await private.raw_open_positions()
            assets = await private.raw_account_assets()
            after = await private.raw_open_positions()
            end = monotonic_ms()
            if (
                type(start) is not int or type(end) is not int
                or end < start or end - start > max_window_ms
            ):
                return failure("PNL_SYNC_STALE_READ_WINDOW")
            observations.append(_one_window(before, assets, after))
    except Exception:
        # The injected GET exception could include an HTTP request path,
        # symbol, balance, or key. Never inspect/serialize it.
        return failure("PNL_SYNC_READ_OR_SCHEMA_UNVERIFIED")

    def count(name: str) -> int:
        return sum(row.get(name) is True for row in observations)

    moved = count("position_pnl_moved")
    stable_match = count("stable_unrealized_reconciled")
    stable_mismatch = count("stable_unrealized_disagreement")
    margin_mismatch = count("margin_mismatch")
    drift = count("position_inventory_drift")
    other = count("non_usdt_exposure")
    if drift:
        status = "PNL_SYNC_INVENTORY_DRIFT_OBSERVED"
    elif other:
        status = "PNL_SYNC_OTHER_ASSET_EXPOSURE_UNVERIFIED"
    elif margin_mismatch:
        status = "PNL_SYNC_POSITION_MARGIN_DISAGREEMENT"
    elif stable_mismatch:
        status = "PNL_SYNC_STABLE_UNREALIZED_DISAGREEMENT"
    elif moved:
        status = "PNL_SYNC_MOVING_MARK_TO_MARKET_OBSERVED"
    elif stable_match == samples:
        status = "PNL_SYNC_STABLE_SAMPLES_RECONCILED_NONATOMIC"
    else:
        status = "PNL_SYNC_UNCLASSIFIED_DISAGREEMENT"

    return PnlSyncReport(
        status=status,
        sampled_windows=len(observations),
        position_pnl_moved_windows=moved,
        stable_position_pnl_windows=stable_match + stable_mismatch,
        stable_unrealized_disagreement_windows=stable_mismatch,
        stable_unrealized_reconciled_windows=stable_match,
        margin_mismatch_windows=margin_mismatch,
        position_inventory_drift_windows=drift,
        observed_non_usdt_exposure_windows=other,
    )
