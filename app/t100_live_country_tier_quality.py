"""Read-only structural audit of MEXC country-scoped BY_VOLUME tier schedules.

This checks PUBLIC metadata consistency, not account permissions, future tier
stability, exchange liquidation logic, liquidity, or execution safety.
No symbol names, raw limits, prices, keys, orders or other identifiers are
returned. This module has no order, database, or private API path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_api_eligibility_census import (
    COUNTRY_CONTRACT_ENDPOINT, _parse_data,
)

_RISK_FIELDS = (
    "symbol", "state", "apiAllowed", "positionOpenType", "futureType",
    "quoteCoin", "settleCoin", "riskLimitMode", "riskLimitType",
    "riskLevelLimit", "riskLongShortSwitch", "riskBaseVol", "riskIncrVol",
    "riskBaseVolShort", "riskIncrVolShort", "riskIncrMmr", "riskIncrImr",
    "maintenanceMarginRate", "initialMarginRate", "riskLimitCustom",
    "contractSize", "liquidationFeeRate", "minLeverage", "maxLeverage",
    "countryConfigContractMaxLeverage",
)


@dataclass(frozen=True)
class CountryTierQuality:
    status: str
    returned_contract_rows: int | None = None
    api_allowed_cross_usdt_perpetual: int | None = None
    incremental_volume_shapes_valid: int | None = None
    incremental_volume_shapes_invalid: int | None = None
    incremental_invalid_reasons: dict[str, int] | None = None
    custom_volume_shapes_valid: int | None = None
    custom_volume_shapes_invalid: int | None = None
    unsupported_or_unknown_shapes: int | None = None
    public_snapshots_consistent: bool = False
    account_specific_api_order_access_certified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _num(raw: Any, *, positive: bool = False) -> Decimal:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_NUMBER") from None
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise ValueError("INVALID_NUMBER")
    return value


def _leverage(row: dict[str, Any]) -> None:
    minimum, maximum = row.get("minLeverage"), row.get("maxLeverage")
    country = row.get("countryConfigContractMaxLeverage", 0)
    if (
        type(minimum) is not int or type(maximum) is not int
        or type(country) is not int or not 1 <= minimum <= maximum
        or country < 0 or (country > 0 and country < minimum)
    ):
        raise ValueError("LEVERAGE_UNVERIFIED")


def _incremental_issue(row: dict[str, Any]) -> str:
    """Return an allowlisted, symbol-free failure category or OK.

    These are schema/structural diagnostics, not a venue-certified model.
    Never expose raw contract data in this result.
    """
    try:
        _leverage(row)
    except ValueError:
        return "LEVERAGE_FIELDS_UNVERIFIED"
    levels, side = row.get("riskLevelLimit"), row.get("riskLongShortSwitch")
    if (
        type(levels) is not int or not 1 <= levels <= 100
        or type(side) is not int or side not in (0, 1)
    ):
        return "LEVEL_OR_SIDE_UNVERIFIED"
    base_name = "riskBaseVolShort" if side else "riskBaseVol"
    step_name = "riskIncrVolShort" if side else "riskIncrVol"
    try:
        base = _num(row.get(base_name), positive=True)
        step = _num(row.get(step_name))
    except ValueError:
        return "VOLUME_FIELDS_UNVERIFIED"
    try:
        mmr = _num(row.get("maintenanceMarginRate"), positive=True)
        imr = _num(row.get("initialMarginRate"), positive=True)
        dm = _num(row.get("riskIncrMmr"))
        di = _num(row.get("riskIncrImr"))
    except ValueError:
        return "MARGIN_RATE_FIELDS_UNVERIFIED"
    try:
        _num(row.get("contractSize"), positive=True)
        fee = _num(row.get("liquidationFeeRate"))
    except ValueError:
        return "CONTRACT_OR_FEE_FIELDS_UNVERIFIED"
    if levels > 1 and step <= 0:
        return "TIER_STEP_INCOHERENT"
    if fee >= 1:
        return "LIQUIDATION_FEE_INCOHERENT"
    for index in range(levels):
        if base + index * step <= 0:
            return "VOLUME_THRESHOLD_INCOHERENT"
        if not 0 < mmr + index * dm <= imr + index * di < 1:
            return "MARGIN_RATE_SCHEDULE_INCOHERENT"
    return "OK"


def _incremental(row: dict[str, Any]) -> bool:
    return _incremental_issue(row) == "OK"

def _custom(row: dict[str, Any]) -> bool:
    try:
        _leverage(row)
        _num(row.get("contractSize"), positive=True)
        fee = _num(row.get("liquidationFeeRate"))
        if not 0 <= fee < 1:
            return False
        tiers = row.get("riskLimitCustom")
        if type(tiers) is not list or not 2 <= len(tiers) <= 100:
            return False
        prior_cap = Decimal(0)
        prior_mmr = Decimal(0)
        prior_imr = Decimal(0)
        prior_lev = 1_000_000
        for i, tier in enumerate(tiers, 1):
            if type(tier) is not dict or type(tier.get("level")) is not int or tier["level"] != i:
                return False
            cap = _num(tier.get("maxVol"), positive=True)
            mmr = _num(tier.get("mmr"), positive=True)
            imr = _num(tier.get("imr"), positive=True)
            maxlev = tier.get("maxLeverage")
            if (
                type(maxlev) is not int or maxlev < 1
                or cap <= prior_cap or not 0 < mmr <= imr < 1
                or mmr < prior_mmr or imr < prior_imr
                or maxlev > prior_lev
            ):
                return False
            prior_cap, prior_mmr, prior_imr, prior_lev = cap, mmr, imr, maxlev
        return True
    except ValueError:
        return False


def assess_country_tier_quality(first_envelope: Any, second_envelope: Any) -> CountryTierQuality:
    def blocked(status: str) -> CountryTierQuality:
        return CountryTierQuality(status=status)

    try:
        first = tuple(sorted(_parse_data(first_envelope), key=lambda r: r["symbol"]))
        second = tuple(sorted(_parse_data(second_envelope), key=lambda r: r["symbol"]))
        def fingerprint(rows: tuple[dict[str, Any], ...]) -> str:
            canonical = [{k: row.get(k) for k in _RISK_FIELDS} for row in rows]
            raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if fingerprint(first) != fingerprint(second):
            return blocked("COUNTRY_TIER_QUALITY_PUBLIC_METADATA_DRIFT")
    except Exception:
        return blocked("COUNTRY_TIER_QUALITY_PUBLIC_SCHEMA_UNVERIFIED")

    incr_ok = incr_bad = custom_ok = custom_bad = unsupported = eligible = 0
    invalid_reasons: dict[str, int] = {}
    for row in second:
        if not (
            type(row.get("state")) is int and row["state"] == 0
            and row.get("apiAllowed") is True
            and type(row.get("positionOpenType")) is int
            and row["positionOpenType"] in (2, 3)
            and type(row.get("futureType")) is int
            and row["futureType"] == 1
            and row.get("quoteCoin") == "USDT"
            and row.get("settleCoin") == "USDT"
        ):
            continue
        eligible += 1
        mode, limit_type = row.get("riskLimitMode"), row.get("riskLimitType")
        if mode == "INCREASE" and limit_type == "BY_VOLUME":
            reason = _incremental_issue(row)
            if reason == "OK":
                incr_ok += 1
            else:
                incr_bad += 1
                invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
        elif mode == "CUSTOM" and limit_type == "BY_VOLUME":
            if _custom(row):
                custom_ok += 1
            else:
                custom_bad += 1
        else:
            unsupported += 1
    return CountryTierQuality(
        status="COUNTRY_TIER_QUALITY_CURRENT_PUBLIC_SHAPES_NONAUTHORIZING",
        returned_contract_rows=len(second),
        api_allowed_cross_usdt_perpetual=eligible,
        incremental_volume_shapes_valid=incr_ok,
        incremental_volume_shapes_invalid=incr_bad,
        incremental_invalid_reasons=dict(sorted(invalid_reasons.items())),
        custom_volume_shapes_valid=custom_ok,
        custom_volume_shapes_invalid=custom_bad,
        unsupported_or_unknown_shapes=unsupported,
        public_snapshots_consistent=True,
    )


async def inspect_country_tier_quality(client: Any) -> CountryTierQuality:
    if client is None:
        return CountryTierQuality(status="COUNTRY_TIER_QUALITY_INVALID_CLIENT")
    snapshots = []
    for _ in range(2):
        try:
            response = await client.get(
                COUNTRY_CONTRACT_ENDPOINT, headers={"Cache-Control": "no-cache"},
            )
            response.raise_for_status()
            snapshots.append(response.json())
        except Exception:
            return CountryTierQuality(status="COUNTRY_TIER_QUALITY_PUBLIC_GET_FAILED")
    return assess_country_tier_quality(snapshots[0], snapshots[1])
