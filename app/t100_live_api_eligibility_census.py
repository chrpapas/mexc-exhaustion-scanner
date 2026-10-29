"""Public GET-only, country-endpoint current API eligibility census.

A CURRENT public metadata snapshot is not a point-in-time historical universe
or proof of account-specific execution permission, liquidity, native stops,
capacity, collateral or liquidation safety.

No credentials, authenticated requests, database, order submission, symbol
listing, or Render deployment. Two GETs, no retry, fail closed on drift.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

COUNTRY_CONTRACT_ENDPOINT = "/api/v1/contract/detail/country"
_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_STATIC_FIELDS = (
    "symbol", "state", "apiAllowed", "positionOpenType", "futureType",
    "quoteCoin", "settleCoin", "riskLimitMode", "riskLimitType",
    "riskLevelLimit", "riskLimitCustom", "riskLongShortSwitch",
    "riskBaseVol", "riskBaseVolShort", "riskIncrVol", "riskIncrVolShort",
    "riskIncrMmr", "riskIncrImr", "maintenanceMarginRate",
    "initialMarginRate", "contractSize", "liquidationFeeRate",
)


@dataclass(frozen=True)
class CountryApiCensus:
    status: str
    returned_contract_rows: int | None = None
    usdt_perpetual_cross_supported: int | None = None
    api_allowed_now: int | None = None
    api_denied_now: int | None = None
    api_permission_unknown: int | None = None
    api_allowed_incremental_by_volume: int | None = None
    api_allowed_incremental_one_tier: int | None = None
    api_allowed_incremental_multi_tier: int | None = None
    api_allowed_custom_by_volume: int | None = None
    api_allowed_custom_multi_tier: int | None = None
    api_allowed_unsupported_or_unknown_mode: int | None = None
    country_metadata_two_get_consistent: bool = False
    endpoint_is_country_scoped: bool = True
    current_public_metadata_only: bool = True
    historical_api_eligibility_certified: bool = False
    account_specific_api_order_access_certified: bool = False
    exchange_atomicity_verified: bool = False
    liquidity_or_capacity_certified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _parse_data(envelope: Any) -> tuple[dict[str, Any], ...]:
    """Reject singleton/partial responses. No claim of global completeness."""
    if (
        type(envelope) is not dict or envelope.get("success") is not True
        or type(envelope.get("code")) is not int
        or envelope["code"] != 0
    ):
        raise ValueError("COUNTRY_RESPONSE_ENVELOPE_UNVERIFIED")
    data = envelope.get("data")
    if type(data) is list:
        rows = data
    elif type(data) is dict and all(
        type(k) is str and type(v) is dict and v.get("symbol") == k
        for k, v in data.items()
    ):
        rows = list(data.values())
    else:
        raise ValueError("COUNTRY_RESPONSE_NOT_UNIVERSE")
    # A singleton cannot establish a country-universe census.
    if not 2 <= len(rows) <= 5000:
        raise ValueError("COUNTRY_RESPONSE_NOT_UNIVERSE")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in rows:
        if type(item) is not dict:
            raise ValueError("COUNTRY_RESPONSE_ROW_UNVERIFIED")
        symbol = item.get("symbol")
        if (
            type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None
            or symbol in seen
        ):
            raise ValueError("COUNTRY_RESPONSE_SYMBOL_UNVERIFIED")
        seen.add(symbol)
        result.append(item)
    return tuple(result)


def _digest(rows: tuple[dict[str, Any], ...]) -> str:
    reduced = [
        {key: row.get(key) for key in _STATIC_FIELDS}
        for row in rows
    ]
    blob = json.dumps(
        reduced, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def summarize_country_api_contracts(envelope1: Any, envelope2: Any) -> CountryApiCensus:
    def blocked(status: str) -> CountryApiCensus:
        return CountryApiCensus(status=status)

    try:
        rows1 = _parse_data(envelope1)
        rows2 = _parse_data(envelope2)
        # Order of response rows must not matter, only symbol-scoped metadata.
        first = tuple(sorted(rows1, key=lambda r: r["symbol"]))
        second = tuple(sorted(rows2, key=lambda r: r["symbol"]))
        if _digest(first) != _digest(second):
            return blocked("COUNTRY_API_CENSUS_METADATA_DRIFT")
    except Exception:
        return blocked("COUNTRY_API_CENSUS_SCHEMA_UNVERIFIED")

    active = allowed = denied = unknown = 0
    incr = incr_one = incr_multi = custom = custom_multi = other = 0
    for row in second:
        # This is a public-only candidate filter, not current user-account
        # ability to place or exit an API trade. Country endpoint can differ
        # by IP/account context; private account/contract checks stay needed.
        eligible_basics = all((
            type(row.get("state")) is int and row["state"] == 0,
            type(row.get("futureType")) is int and row["futureType"] == 1,
            type(row.get("positionOpenType")) is int
            and row["positionOpenType"] in (2, 3),
            row.get("quoteCoin") == "USDT",
            row.get("settleCoin") == "USDT",
        ))
        if not eligible_basics:
            continue
        active += 1
        access = row.get("apiAllowed")
        if access is False:
            denied += 1
            continue
        if access is not True:
            unknown += 1
            continue
        allowed += 1
        mode = row.get("riskLimitMode")
        typ = row.get("riskLimitType")
        if mode == "INCREASE" and typ == "BY_VOLUME":
            incr += 1
            levels = row.get("riskLevelLimit")
            if type(levels) is int and levels == 1:
                incr_one += 1
            elif type(levels) is int and levels >= 2:
                incr_multi += 1
            else:
                other += 1
        elif mode == "CUSTOM" and typ == "BY_VOLUME":
            custom += 1
            tiers = row.get("riskLimitCustom")
            if type(tiers) is list and len(tiers) >= 2:
                custom_multi += 1
        else:
            other += 1

    return CountryApiCensus(
        status="COUNTRY_API_CENSUS_CURRENT_PUBLIC_COUNTS_NONAUTHORIZING",
        returned_contract_rows=len(second),
        usdt_perpetual_cross_supported=active,
        api_allowed_now=allowed,
        api_denied_now=denied,
        api_permission_unknown=unknown,
        api_allowed_incremental_by_volume=incr,
        api_allowed_incremental_one_tier=incr_one,
        api_allowed_incremental_multi_tier=incr_multi,
        api_allowed_custom_by_volume=custom,
        api_allowed_custom_multi_tier=custom_multi,
        api_allowed_unsupported_or_unknown_mode=other,
        country_metadata_two_get_consistent=True,
    )


async def inspect_country_api_census(client: Any) -> CountryApiCensus:
    """Two uncached public GETs only, no retry or private endpoint access."""
    if client is None:
        return CountryApiCensus(status="COUNTRY_API_CENSUS_INVALID_CLIENT")
    snapshots = []
    for _ in range(2):
        try:
            response = await client.get(
                COUNTRY_CONTRACT_ENDPOINT,
                headers={"Cache-Control": "no-cache"},
            )
            response.raise_for_status()
            snapshots.append(response.json())
        except Exception:
            return CountryApiCensus(status="COUNTRY_API_CENSUS_PUBLIC_GET_FAILED")
    return summarize_country_api_contracts(snapshots[0], snapshots[1])
