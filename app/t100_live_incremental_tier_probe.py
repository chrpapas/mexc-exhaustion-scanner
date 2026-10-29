"""GET-only, nonauthorizing comparison of a PUBLIC INCREASE/BY_VOLUME
candidate schedule with the CURRENT private short-side MEXC risk tier.

No asset/PnL witness required here: this is an independent diagnostic.
Never claims exchange atomicity, complete future-tier semantics, collateral,
liquidation safety, position ownership, or a live order permit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS
from app.t100_live_incremental_tier_candidate import (
    IncrementalEligibilityShape, classify_incremental_eligibility,
    reconstruct_incremental_research_candidate,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe

CONTRACT_COUNTRY = "/api/v1/contract/detail/country"


@dataclass(frozen=True)
class IncrementalTierProbeReport:
    status: str
    observed_positions: int | None = None
    candidate_mode: str = "INCREASE"
    candidate_type: str = "BY_VOLUME"
    public_candidate_reconstructed: bool = False
    private_current_risk_rows_observed: bool = False
    private_current_mmr_exact_match_count: int = 0
    private_current_mmr_above_candidate_count: int = 0
    private_current_mmr_below_candidate_count: int = 0
    short_specific_limits_used_count: int = 0
    api_trading_denied_contract_count: int = 0
    api_trading_allowed_for_all: bool = False
    candidate_max_level_count: int | None = None
    candidate_failure: str = "NOT_CHECKED"
    contract_state: str = "NOT_CHECKED"
    contract_api_order_access: str = "NOT_CHECKED"
    contract_cross_margin_support: str = "NOT_CHECKED"
    contract_duration: str = "NOT_CHECKED"
    contract_quote_currency: str = "NOT_CHECKED"
    contract_settlement_currency: str = "NOT_CHECKED"
    position_bookends_consistent: bool = False
    public_private_snapshot_atomic: bool = False
    future_stressed_tiers_certified: bool = False
    cross_wallet_collateral_certified: bool = False
    account_wide_completeness_certified: bool = False
    venue_liquidation_certified: bool = False
    native_stop_protection_verified: bool = False
    t100_position_ownership_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


_SAFE_CANDIDATE_FAILURES = frozenset({
    "INCR_PUBLIC_SCHEMA_UNVERIFIED",
    "INCR_MODE_OR_TYPE_UNSUPPORTED",
    "INCR_CONTRACT_NOT_ELIGIBLE",
    "INCR_LEVEL_COUNT_UNVERIFIED",
    "INCR_SIDE_MODE_UNVERIFIED",
    "INCR_SHORT_SIDE_FIELDS_MISSING",
    "INCR_NUMERIC_FIELDS_UNVERIFIED",
    "INCR_PUBLIC_SCHEDULE_INCOHERENT",
    "INCR_POSITION_OUTSIDE_PUBLISHED_CANDIDATE",
    "INCR_PRIVATE_CROSS_SHORT_UNVERIFIED",
    "INCR_PRIVATE_LEVEL_OUTSIDE_PUBLIC_SCHEDULE",
    "INCR_PRIVATE_FIELDS_UNVERIFIED",
})


def _inventory(rows: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if (
        type(rows) is not list or not 1 <= len(rows) <= 4
        or any(type(r) is not dict for r in rows)
    ):
        raise ValueError("INVALID_INVENTORY")
    symbols = [r.get("symbol") for r in rows]
    if any(
        type(s) is not str or re.fullmatch(r"[A-Z0-9]+_USDT", s) is None
        for s in symbols
    ) or len(set(symbols)) != len(symbols):
        raise ValueError("INVALID_SYMBOLS")
    key = tuple(sorted(json.dumps(
        {field: row[field] for field in CRITICAL_POSITION_FIELDS if field in row},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) for row in rows))
    return tuple(sorted(symbols)), key


async def inspect_incremental_tier_candidate(
    private: ReadOnlyMexcProbe, public: Any,
) -> IncrementalTierProbeReport:
    """No retry, <= 2 account positions + 4 private tier + 4 public GETs."""
    def blocked(
        status: str, *, failure: str = "NOT_CHECKED",
        public_eligibility: IncrementalEligibilityShape | None = None,
    ) -> IncrementalTierProbeReport:
        if failure not in _SAFE_CANDIDATE_FAILURES:
            failure = "NOT_CHECKED"
        # Only expose the fixed PUBLIC metadata categories when the exact
        # contract-eligibility gate rejected a stable position bookend.
        result = (
            public_eligibility if
            failure == "INCR_CONTRACT_NOT_ELIGIBLE" and
            type(public_eligibility) is IncrementalEligibilityShape
            else None
        )
        return IncrementalTierProbeReport(
            status=status, candidate_failure=failure,
            contract_state=result.state if result else "NOT_CHECKED",
            contract_api_order_access=result.api_order_access if result else "NOT_CHECKED",
            contract_cross_margin_support=result.cross_margin_support if result else "NOT_CHECKED",
            contract_duration=result.contract_duration if result else "NOT_CHECKED",
            contract_quote_currency=result.quote_currency if result else "NOT_CHECKED",
            contract_settlement_currency=result.settlement_currency if result else "NOT_CHECKED",
            position_bookends_consistent=result is not None,
        )

    if not isinstance(private, ReadOnlyMexcProbe) or public is None:
        return blocked("INCR_PROBE_CONFIGURATION_INVALID")
    try:
        before = await private.raw_open_positions()
        symbols, first_key = _inventory(before)
    except Exception:
        return blocked("INCR_PROBE_FIRST_INVENTORY_UNVERIFIED")

    pub_by_symbol: dict[str, dict[str, Any]] = {}
    private_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            risk = await private.raw_account_risk_limits(symbol)
            if set(risk) != {symbol}:
                return blocked("INCR_PROBE_PRIVATE_RISK_SCOPE_UNVERIFIED")
            shorts = [
                item for item in risk[symbol]
                if type(item) is dict and item.get("positionType") == 2
            ]
            if len(shorts) != 1 or shorts[0].get("symbol") != symbol:
                return blocked("INCR_PROBE_PRIVATE_SHORT_UNVERIFIED")
            private_by_symbol[symbol] = shorts[0]
        except Exception:
            return blocked("INCR_PROBE_PRIVATE_RISK_GET_FAILED")
        try:
            r = await public.get(CONTRACT_COUNTRY, params={"symbol": symbol})
            r.raise_for_status()
            envelope = r.json()
        except Exception:
            return blocked("INCR_PROBE_PUBLIC_CONTRACT_GET_FAILED")
        if (
            type(envelope) is not dict
            or envelope.get("success") is not True
            or type(envelope.get("code")) is not int
            or envelope.get("code") != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol
        ):
            return blocked("INCR_PROBE_PUBLIC_CONTRACT_SCHEMA_UNVERIFIED")
        pub_by_symbol[symbol] = envelope["data"]

    try:
        after = await private.raw_open_positions()
        after_symbols, last_key = _inventory(after)
    except Exception:
        return blocked("INCR_PROBE_FINAL_INVENTORY_UNVERIFIED")
    if symbols != after_symbols or first_key != last_key:
        return blocked("INCR_PROBE_CRITICAL_INVENTORY_DRIFT")

    exact = above = below = short_specific = denied = 0
    max_levels = 0
    for position in after:
        symbol = position["symbol"]
        candidate = reconstruct_incremental_research_candidate(
            pub_by_symbol[symbol],
            position_contracts=position.get("holdVol"),
            private_short_risk=private_by_symbol[symbol],
        )
        if candidate.status != "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING":
            return blocked(
                "INCR_PROBE_CANDIDATE_UNVERIFIED",
                failure=candidate.status,
                public_eligibility=classify_incremental_eligibility(
                    pub_by_symbol[symbol]
                ) if candidate.status == "INCR_CONTRACT_NOT_ELIGIBLE" else None,
            )
        if not candidate.public_contract_eligible:
            # API order permission is a HARD execution blocker, never a
            # reason to fake public metadata or skip a risk calculation.
            if classify_incremental_eligibility(pub_by_symbol[symbol]).api_order_access != "DENIED":
                return blocked("INCR_PROBE_API_ACCESS_UNVERIFIED")
            denied += 1
        max_levels = max(max_levels, len(candidate.tiers))
        short_specific += int(candidate.short_specific_limits_used)
        comparison = candidate.private_current_comparison
        exact += int(comparison == "PRIVATE_CURRENT_MMR_EXACT_MATCH")
        above += int(comparison == "PRIVATE_CURRENT_MMR_ABOVE_CANDIDATE")
        below += int(comparison == "PRIVATE_CURRENT_MMR_BELOW_CANDIDATE")

    status = (
        "INCR_PROBE_API_TRADING_DENIED_RISK_RESEARCH_ONLY"
        if denied else
        "INCR_PROBE_CURRENT_PRIVATE_BELOW_CANDIDATE_NONAUTHORIZING"
        if below else
        "INCR_PROBE_PUBLIC_CANDIDATE_PRIVATE_COMPARED_NONAUTHORIZING"
    )
    return IncrementalTierProbeReport(
        status=status, observed_positions=len(after),
        public_candidate_reconstructed=True,
        private_current_risk_rows_observed=True,
        private_current_mmr_exact_match_count=exact,
        private_current_mmr_above_candidate_count=above,
        private_current_mmr_below_candidate_count=below,
        short_specific_limits_used_count=short_specific,
        api_trading_denied_contract_count=denied,
        api_trading_allowed_for_all=denied == 0,
        contract_api_order_access="DENIED" if denied == len(after) else (
            "MIXED" if denied else "ALLOWED"
        ),
        candidate_max_level_count=max_levels,
        position_bookends_consistent=True,
    )
