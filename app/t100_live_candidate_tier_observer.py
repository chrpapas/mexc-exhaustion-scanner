"""Read-only proposed T100 SHORT candidate: public/private CURRENT volume-tier witness.

Research-only observation: signed private GET /account/risk_limit for one
explicit symbol and two public GET /contract/detail/country. This does NOT
certify future tiers, account-wide collateral, API order permission, liquidation,
native stops, execution capability, or live authorization. No orders/POST/DB.
No raw symbols, margin rates, quantities, credentials, prices or URLs output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_incremental_tier_candidate import (
    classify_incremental_eligibility, reconstruct_incremental_candidate,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_risk_tier_witness import assess_risk_tier_witness

_COUNTRY = "/api/v1/contract/detail/country"
_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_PUBLIC_FIELDS = (
    "symbol", "state", "apiAllowed", "positionOpenType", "futureType",
    "quoteCoin", "settleCoin", "riskLimitMode", "riskLimitType",
    "riskLongShortSwitch", "riskLevelLimit", "riskBaseVol", "riskIncrVol",
    "riskBaseVolShort", "riskIncrVolShort", "riskIncrMmr", "riskIncrImr",
    "maintenanceMarginRate", "initialMarginRate", "riskLimitCustom",
    "contractSize", "liquidationFeeRate", "minLeverage", "maxLeverage",
    "countryConfigContractMaxLeverage",
)
_PRIVATE_FIELDS = (
    "symbol", "positionType", "level", "openType", "maxVol", "mmr",
    "imr", "currentMmr", "maxLeverage", "leverage", "limitBySys",
)
_ALLOWED_OUTCOMES = frozenset({
    "INCR_PUBLIC_SCHEMA_UNVERIFIED", "INCR_CONTRACT_NOT_ELIGIBLE",
    "INCR_MODE_OR_TYPE_UNSUPPORTED", "INCR_LEVEL_COUNT_UNVERIFIED",
    "INCR_SIDE_MODE_UNVERIFIED", "INCR_SHORT_SIDE_FIELDS_MISSING",
    "INCR_NUMERIC_FIELDS_UNVERIFIED", "INCR_PUBLIC_SCHEDULE_INCOHERENT",
    "INCR_POSITION_OUTSIDE_PUBLISHED_CANDIDATE",
    "INCR_PRIVATE_CROSS_SHORT_UNVERIFIED",
    "INCR_PRIVATE_LEVEL_OUTSIDE_PUBLIC_SCHEDULE",
    "INCR_PRIVATE_FIELDS_UNVERIFIED",
    "RISK_TIER_SYMBOL_INVALID", "RISK_TIER_SYMBOL_RESPONSE_MISMATCH",
    "RISK_TIER_PRIVATE_ROWS_MISSING", "RISK_TIER_PRIVATE_SHORT_NOT_UNIQUE",
    "RISK_TIER_PRIVATE_SYMBOL_DRIFT", "RISK_TIER_PRIVATE_CROSS_OR_CAP_UNVERIFIED",
    "RISK_TIER_PRIVATE_MMR_INVALID", "RISK_TIER_CURRENT_MMR_INVALID",
    "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE",
    "RISK_TIER_PUBLIC_CONTRACT_NOT_ELIGIBLE",
    "RISK_TIER_LIQUIDATION_FEE_INVALID", "RISK_TIER_PUBLIC_LEVERAGE_LIMIT",
    "RISK_TIER_CUSTOM_LEVELS_MISSING", "RISK_TIER_LEVEL_SEQUENCE_INVALID",
    "RISK_TIER_LEVEL_MONOTONICITY_INVALID",
    "RISK_TIER_PRIVATE_LEVEL_NOT_IN_SCHEDULE",
    "RISK_TIER_PRIVATE_PUBLIC_MMR_CONFLICT",
    "RISK_TIER_POSITION_EXCEEDS_PUBLISHED_TIERS",
    "RISK_TIER_POSITION_LEVERAGE_EXCEEDS_TIER", "RISK_TIER_INPUT_INVALID",
})


@dataclass(frozen=True)
class CandidateTierObservation:
    status: str
    schedule_mode: str = "NOT_CHECKED"
    public_contract_eligible: bool = False
    public_metadata_two_get_consistent: bool = False
    private_short_tier_observed: bool = False
    private_risk_two_get_consistent: bool = False
    private_cross_mode_observed: bool = False
    candidate_quantity_evaluated: bool = False
    current_private_mmr_not_below_public: bool = False
    private_current_volume_cap_covers_candidate: bool = False
    higher_tier_requires_recheck: bool = False
    failure_category: str = "NOT_CHECKED"
    manual_positions_untouched: bool = True
    account_specific_api_order_access_certified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _positive(raw: Any) -> Decimal:
    if type(raw) is bool or raw is None:
        raise ValueError("INVALID_INPUT")
    try:
        number = Decimal(str(raw))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError("INVALID_INPUT") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("INVALID_INPUT")
    return number


def _stable_payload(first: Any, second: Any, fields: tuple[str, ...]) -> bool:
    def canonical(value: Any) -> str:
        if type(value) is not dict:
            raise ValueError("INVALID_PAYLOAD")
        return json.dumps({k: value.get(k) for k in fields},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)
    return canonical(first) == canonical(second)


async def inspect_candidate_tier(
    private: ReadOnlyMexcProbe, public: Any,
    symbol: str, *, proposed_contracts: Any,
) -> CandidateTierObservation:
    def block(status: str, **kwargs: Any) -> CandidateTierObservation:
        return CandidateTierObservation(status=status, **kwargs)

    if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
        return block("CANDIDATE_TIER_SYMBOL_INVALID")
    if not isinstance(private, ReadOnlyMexcProbe) or public is None:
        return block("CANDIDATE_TIER_CLIENT_INVALID")
    try:
        volume = _positive(proposed_contracts)
    except ValueError:
        return block("CANDIDATE_TIER_QUANTITY_INVALID")

    public_rows: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            response = await public.get(_COUNTRY, params={"symbol": symbol})
            response.raise_for_status()
            envelope = response.json()
        except Exception:
            return block("CANDIDATE_TIER_PUBLIC_GET_FAILED")
        if (type(envelope) is not dict or envelope.get("success") is not True
            or type(envelope.get("code")) is not int or envelope["code"] != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol):
            return block("CANDIDATE_TIER_PUBLIC_SCHEMA_UNVERIFIED")
        public_rows.append(envelope["data"])
    try:
        if not _stable_payload(public_rows[0], public_rows[1], _PUBLIC_FIELDS):
            return block("CANDIDATE_TIER_PUBLIC_METADATA_DRIFT")
    except (TypeError, ValueError):
        return block("CANDIDATE_TIER_PUBLIC_SCHEMA_UNVERIFIED")

    contract = public_rows[1]
    mode = contract.get("riskLimitMode")
    mode = mode if type(mode) is str and mode in ("CUSTOM", "INCREASE") else "UNKNOWN"
    shared = dict(
        schedule_mode=mode, public_metadata_two_get_consistent=True,
    )
    if not classify_incremental_eligibility(contract).contract_eligible:
        return block("CANDIDATE_TIER_PUBLIC_INELIGIBLE", **shared)
    shared["public_contract_eligible"] = True
    if contract.get("riskLimitType") != "BY_VOLUME" or mode == "UNKNOWN":
        return block("CANDIDATE_TIER_VOLUME_MODE_UNSUPPORTED", **shared)

    private_rows: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            scoped = await private.raw_account_risk_limits(symbol)
        except Exception:
            return block("CANDIDATE_TIER_PRIVATE_GET_FAILED", **shared)
        if set(scoped) != {symbol}:
            return block("CANDIDATE_TIER_PRIVATE_SCOPE_UNVERIFIED", **shared)
        rows = scoped[symbol]
        shorts = [r for r in rows if type(r) is dict and
                  type(r.get("positionType")) is int and r["positionType"] == 2]
        if len(shorts) != 1 or shorts[0].get("symbol") != symbol:
            return block("CANDIDATE_TIER_PRIVATE_SHORT_UNVERIFIED", **shared)
        private_rows.append(shorts[0])
    shared["private_short_tier_observed"] = True
    try:
        if not _stable_payload(private_rows[0], private_rows[1], _PRIVATE_FIELDS):
            return block("CANDIDATE_TIER_PRIVATE_RISK_DRIFT", **shared)
    except (ValueError, TypeError):
        return block("CANDIDATE_TIER_PRIVATE_SCHEMA_UNVERIFIED", **shared)
    shared["private_risk_two_get_consistent"] = True
    short = private_rows[1]
    if type(short.get("openType")) is not int or short["openType"] != 2:
        return block("CANDIDATE_TIER_PRIVATE_CROSS_NOT_OBSERVED", **shared)
    shared["private_cross_mode_observed"] = True

    if mode == "INCREASE":
        outcome = reconstruct_incremental_candidate(
            contract, position_contracts=volume, private_short_risk=short,
        )
        success = outcome.status == "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING"
        if not success:
            return block("CANDIDATE_TIER_RECONSTRUCTION_BLOCKED",
                         failure_category=outcome.status if outcome.status in _ALLOWED_OUTCOMES
                         else "UNCLASSIFIED_TIER_FAILURE", **shared)
        mmr_ok = outcome.private_current_comparison in {
            "PRIVATE_CURRENT_MMR_EXACT_MATCH",
            "PRIVATE_CURRENT_MMR_ABOVE_CANDIDATE",
        }
        modeled_level = outcome.selected_position_tier
    else:
        outcome = assess_risk_tier_witness(
            symbol=symbol, contracts=volume,
            private_risk={symbol: [short]}, public_contract=contract,
        )
        success = outcome.status == (
            "CURRENT_CUSTOM_VOLUME_RISK_SCHEDULE_OBSERVED_NONAUTHORIZING"
        )
        if not success:
            return block("CANDIDATE_TIER_RECONSTRUCTION_BLOCKED",
                         failure_category=outcome.status if outcome.status in _ALLOWED_OUTCOMES
                         else "UNCLASSIFIED_TIER_FAILURE", **shared)
        mmr_ok = True
        modeled_level = outcome.modeled_volume_tier

    try:
        cap_ok = volume <= _positive(short.get("maxVol"))
        current_level = short.get("level")
        if type(current_level) is not int or current_level <= 0:
            raise ValueError("INVALID_TIER")
    except ValueError:
        return block("CANDIDATE_TIER_PRIVATE_CAP_UNVERIFIED", **shared)

    extra = dict(
        **shared, candidate_quantity_evaluated=True,
        current_private_mmr_not_below_public=mmr_ok,
        private_current_volume_cap_covers_candidate=cap_ok,
        higher_tier_requires_recheck=modeled_level > current_level,
    )
    if not mmr_ok:
        return block("CANDIDATE_TIER_PRIVATE_MMR_BELOW_PUBLIC", **extra)
    if not cap_ok:
        return block("CANDIDATE_TIER_PRIVATE_CAP_BELOW_CANDIDATE", **extra)
    if modeled_level > current_level:
        return block("CANDIDATE_TIER_PROSPECTIVE_LEVEL_RECHECK_REQUIRED", **extra)
    return block("CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING", **extra)
