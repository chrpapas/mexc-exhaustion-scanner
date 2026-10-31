"""Bounded, redacted GET-only follow-up for a MEXC PNL aggregation mismatch.

The asset endpoint and all-position endpoint are NOT atomic, so a strict
pairwise comparison can fail as fair prices move. A second close pair can
distinguish unstable PNL readings from a repeatedly stable disagreement,
without blessing either as account-wide collateral evidence.

This probe never returns raw rows, prices, balances, symbols, position IDs,
timestamps or account data. It never certifies risk, controls a trade,
performs a POST, or retries an ambiguous mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


@dataclass(frozen=True)
class PnlPairRecheck:
    status: str
    pairs_observed: int = 0
    positions_stable_across_pairs: bool = False
    position_unrealized_moved_across_pairs: bool = False
    asset_unrealized_moved_across_pairs: bool = False
    first_pair_reconciled: bool = False
    second_pair_reconciled: bool = False
    risk_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _num(raw: Any) -> Decimal:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_PNL")
    try:
        val = Decimal(str(raw))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError("INVALID_PNL") from None
    if not val.is_finite():
        raise ValueError("INVALID_PNL")
    return val


def _same(a: Decimal, b: Decimal) -> bool:
    return abs(a-b) <= max(
        Decimal("0.00000001"),
        max(abs(a), abs(b)) * Decimal("0.00000001"),
    )


def _position_snapshot(rows: Any) -> tuple[tuple[Any, ...], Decimal]:
    if type(rows) is not list or not 1 <= len(rows) <= 4:
        raise ValueError("INVALID_POSITIONS")
    keys = []
    pnl = Decimal(0)
    for row in rows:
        if type(row) is not dict:
            raise ValueError("INVALID_POSITION")
        pid = row.get("positionId") or row.get("id")
        symbol = row.get("symbol")
        side = row.get("positionType")
        mode = row.get("openType")
        if (
            type(symbol) is not str or not symbol
            or pid is None or type(pid) is bool
            or type(side) is not int or side not in {1, 2}
            or type(mode) is not int or mode not in {1, 2}
        ):
            raise ValueError("INVALID_POSITION_IDENTITY")
        keys.append((
            str(pid), symbol, side, mode,
            _num(row.get("holdVol")), _num(row.get("im")),
        ))
        pnl += _num(row.get("unRealizedPnl"))
    if len({k[0] for k in keys}) != len(keys):
        raise ValueError("DUPLICATE_POSITION")
    return tuple(sorted(keys)), pnl


def _asset_pnl(rows: Any) -> Decimal:
    if type(rows) is not list:
        raise ValueError("INVALID_ASSETS")
    usdt = [r for r in rows if type(r) is dict and r.get("currency") == "USDT"]
    if len(usdt) != 1:
        raise ValueError("USDT_ASSET_NOT_UNIQUE")
    return _num(usdt[0].get("unrealized"))


async def recheck_pnl_pair_readonly(
    private: ReadOnlyMexcProbe,
) -> PnlPairRecheck:
    """Two bounded position->asset GET pairs; never upgrades certification."""
    if not isinstance(private, ReadOnlyMexcProbe):
        return PnlPairRecheck(status="PNL_RECHECK_PROBE_INVALID")
    try:
        pairs = []
        for _ in range(2):
            positions = await private.raw_open_positions()
            assets = await private.raw_account_assets()
            identity, pnl = _position_snapshot(positions)
            aggregate = _asset_pnl(assets)
            witness = assess_position_margin_witness(positions, assets)
            pairs.append((
                identity, pnl, aggregate,
                witness.get("position_inventory_economic_witness_observed") is True,
                witness.get("usdt_position_margin_reconciled") is True,
                witness.get("usdt_unrealized_reconciled") is True,
            ))
        first, second = pairs
        stable = first[0] == second[0]
        moved_positions = not _same(first[1], second[1])
        moved_asset = not _same(first[2], second[2])
        first_ok, second_ok = first[3], second[3]
        common = dict(
            pairs_observed=2,
            positions_stable_across_pairs=stable,
            position_unrealized_moved_across_pairs=moved_positions,
            asset_unrealized_moved_across_pairs=moved_asset,
            first_pair_reconciled=first_ok,
            second_pair_reconciled=second_ok,
        )
        if not stable:
            status = "PNL_RECHECK_POSITION_IDENTITY_OR_MARGIN_DRIFT"
        elif moved_positions or moved_asset:
            status = "PNL_RECHECK_MARK_TO_MARKET_MOVED_NONAUTHORIZING"
        elif first_ok and second_ok:
            status = "PNL_RECHECK_TWO_PAIRS_MATCH_NONAUTHORIZING"
        elif not first[4] or not second[4]:
            status = "PNL_RECHECK_MARGIN_DISAGREEMENT_NONAUTHORIZING"
        elif not first[5] and not second[5]:
            status = "PNL_RECHECK_PERSISTENT_PNL_MISMATCH_NONAUTHORIZING"
        else:
            status = "PNL_RECHECK_INTERMITTENT_PARITY_NONAUTHORIZING"
        return PnlPairRecheck(status=status, **common)
    except Exception:
        # Never reveal private error, account values, or input response.
        return PnlPairRecheck(status="PNL_RECHECK_READ_OR_SCHEMA_UNVERIFIED")
