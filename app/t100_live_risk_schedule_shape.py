"""Redacted classification of MEXC public contract risk-limit schedules.

This is descriptive metadata only: no tier extrapolation, no calculation of
liquidation price, no assumption that public schedule overrides private
account restrictions, and NEVER any live-trading authorization.

MEXC documents: riskLimitMode CUSTOM/INCREASE; riskLimitType
BY_VOLUME/BY_VALUE. Incremental metadata lists riskBaseVol, riskIncrVol,
riskIncrMmr, riskIncrImr and riskLevelLimit. We check *shape*, not whether
incremental schedules safely bound future maintenance margin.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class RiskScheduleShape:
    mode: str = "NOT_CHECKED"
    limit_type: str = "NOT_CHECKED"
    schedule_fields: str = "NOT_CHECKED"
    modeled_future_tiers: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _nonnegative_number(raw: Any) -> Decimal | None:
    if type(raw) is bool or raw is None:
        return None
    try:
        n = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return n if n.is_finite() and n >= 0 else None


def classify_risk_schedule_shapes(
    public_contract_by_symbol: Any,
) -> RiskScheduleShape:
    """Classify 1..4 public MEXC contract rows; never return symbols/values."""
    if (
        not isinstance(public_contract_by_symbol, dict)
        or not 1 <= len(public_contract_by_symbol) <= 4
        or any(type(k) is not str or type(v) is not dict
               for k, v in public_contract_by_symbol.items())
    ):
        return RiskScheduleShape(
            mode="UNKNOWN", limit_type="UNKNOWN", schedule_fields="UNVERIFIED",
        )

    def select(raw: Any, allowed: set[str]) -> str:
        return raw if type(raw) is str and raw in allowed else "UNKNOWN"

    def fields(row: dict[str, Any], mode: str) -> str:
        if mode == "CUSTOM":
            tiers = row.get("riskLimitCustom")
            if (
                isinstance(tiers, list) and bool(tiers)
                and all(type(x) is dict and
                        all(key in x for key in (
                            "level", "maxVol", "mmr", "imr", "maxLeverage"
                        )) for x in tiers)
            ):
                return "CUSTOM_FIELDS_PRESENT"
            return "CUSTOM_FIELDS_MISSING_OR_MALFORMED"
        if mode == "INCREASE":
            base = _nonnegative_number(row.get("riskBaseVol"))
            increment = _nonnegative_number(row.get("riskIncrVol"))
            incr_mmr = _nonnegative_number(row.get("riskIncrMmr"))
            incr_imr = _nonnegative_number(row.get("riskIncrImr"))
            mmr = _nonnegative_number(row.get("maintenanceMarginRate"))
            imr = _nonnegative_number(row.get("initialMarginRate"))
            levels = row.get("riskLevelLimit")
            if (
                base is None or base <= 0
                or increment is None or incr_mmr is None or incr_imr is None
                or mmr is None or imr is None
                or type(levels) is not int or levels < 1
            ):
                return "INCREMENTAL_FIELDS_MISSING_OR_MALFORMED"
            if (
                (levels > 1 and increment <= 0)
                or (levels == 1 and any(v > 0 for v in (
                    increment, incr_mmr, incr_imr,
                )))
                or mmr >= 1 or imr >= 1 or mmr > imr
            ):
                return "INCREMENTAL_FIELDS_INCOHERENT"
            return "INCREMENTAL_FIELDS_PRESENT_UNMODELED"
        return "UNVERIFIED"

    modes: set[str] = set()
    types: set[str] = set()
    statuses: set[str] = set()
    for row in public_contract_by_symbol.values():
        mode = select(row.get("riskLimitMode"), {"CUSTOM", "INCREASE"})
        limit_type = select(
            row.get("riskLimitType"), {"BY_VOLUME", "BY_VALUE"},
        )
        modes.add(mode)
        types.add(limit_type)
        statuses.add(fields(row, mode))

    def aggregate(group: set[str]) -> str:
        if "UNKNOWN" in group:
            return "UNKNOWN"
        return next(iter(group)) if len(group) == 1 else "MIXED"

    return RiskScheduleShape(
        mode=aggregate(modes),
        limit_type=aggregate(types),
        schedule_fields=next(iter(statuses)) if len(statuses) == 1 else "MIXED",
    )
