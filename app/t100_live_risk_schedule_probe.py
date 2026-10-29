"""Independent, bounded MEXC public risk-schedule shape inspection.

GET-only: one account-wide private positions read, at most four country
contract metadata GETs, then a bookend private positions read. No asset
or PnL reconciliation dependency and NO orders, database or Render action.

This identifies PUBLIC risk-limit schema ONLY; it never certifies private
risk-limit tiers, future maintenance margin, cross-wallet collateral,
atomicity or live trading. Exact symbols and numerical values are withheld.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_risk_schedule_shape import classify_risk_schedule_shapes

COUNTRY_CONTRACT_PATH = "/api/v1/contract/detail/country"


@dataclass(frozen=True)
class RiskScheduleProbeReport:
    status: str
    observed_positions: int | None = None
    risk_schedule_mode: str = "NOT_CHECKED"
    risk_schedule_type: str = "NOT_CHECKED"
    risk_schedule_fields: str = "NOT_CHECKED"
    position_bookends_consistent: bool = False
    risk_schedule_two_frame_certified: bool = False
    current_private_risk_tiers_certified: bool = False
    future_stressed_tiers_certified: bool = False
    cross_wallet_collateral_certified: bool = False
    venue_liquidation_certified: bool = False
    account_wide_completeness_certified: bool = False
    exchange_atomicity_verified: bool = False
    read_only: bool = True
    mexc_orders_sent: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _safe_inventory(rows: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if (
        type(rows) is not list or not 1 <= len(rows) <= 4
        or any(type(x) is not dict for x in rows)
    ):
        raise ValueError("INVENTORY_SCOPE")
    symbols = [row.get("symbol") for row in rows]
    if any(
        type(sym) is not str or re.fullmatch(r"[A-Z0-9]+_USDT", sym) is None
        for sym in symbols
    ) or len(set(symbols)) != len(symbols):
        raise ValueError("SYMBOL_SCOPE")
    critical = tuple(sorted(json.dumps(
        {key: r[key] for key in CRITICAL_POSITION_FIELDS if key in r},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) for r in rows))
    return tuple(sorted(symbols)), critical


async def inspect_risk_schedule_shape(
    private: ReadOnlyMexcProbe, public: Any,
) -> RiskScheduleProbeReport:
    """No retry, exactly 2 private positions GET + N public GET if successful."""
    def blocked(status: str) -> RiskScheduleProbeReport:
        return RiskScheduleProbeReport(status=status)

    if not isinstance(private, ReadOnlyMexcProbe) or public is None:
        return blocked("RISK_SHAPE_INVALID_CONFIG")
    try:
        first = await private.raw_open_positions()
    except Exception:
        return blocked("RISK_SHAPE_PRIVATE_GET_FAILED")
    try:
        symbols, before = _safe_inventory(first)
    except Exception:
        return blocked("RISK_SHAPE_INVENTORY_UNVERIFIED")

    contract_rows: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            response = await public.get(
                COUNTRY_CONTRACT_PATH, params={"symbol": symbol},
            )
            response.raise_for_status()
            envelope = response.json()
        except Exception:
            return blocked("RISK_SHAPE_PUBLIC_GET_FAILED")
        if (
            type(envelope) is not dict
            or envelope.get("success") is not True
            or type(envelope.get("code")) is not int
            or envelope["code"] != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol
        ):
            return blocked("RISK_SHAPE_PUBLIC_SCHEMA_UNVERIFIED")
        contract_rows[symbol] = envelope["data"]

    try:
        second = await private.raw_open_positions()
        after_symbols, after = _safe_inventory(second)
    except Exception:
        return blocked("RISK_SHAPE_BOOKEND_UNVERIFIED")
    if symbols != after_symbols or before != after:
        return blocked("RISK_SHAPE_POSITION_BOOKEND_DRIFT")

    shape = classify_risk_schedule_shapes(contract_rows)
    if shape.mode == "UNKNOWN" or shape.limit_type == "UNKNOWN":
        status = "RISK_SHAPE_MODE_OR_TYPE_UNKNOWN_NONAUTHORIZING"
    elif shape.mode == "MIXED" or shape.limit_type == "MIXED":
        status = "RISK_SHAPE_MIXED_MODES_NONAUTHORIZING"
    else:
        status = "RISK_SHAPE_PUBLIC_METADATA_OBSERVED_NONAUTHORIZING"
    return RiskScheduleProbeReport(
        status=status, observed_positions=len(symbols),
        risk_schedule_mode=shape.mode, risk_schedule_type=shape.limit_type,
        risk_schedule_fields=shape.schedule_fields,
        position_bookends_consistent=True,
    )
