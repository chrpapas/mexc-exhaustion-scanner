"""Guarded public-GET-only audit of published V40 volume tier research.

Two independent GETs /api/v1/contract/detail/country?symbol=... .
Uses only public HTTP, never reads or sends MEXC account credentials.
No private endpoints, orders, DB, Render, network after module import,
live authorization, or implicit certification of future rates.
The CLI's REDACTED output omits raw symbols, prices and quantities.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
import re
from typing import Any, Mapping

from app.t100_live_public_v40_tier_stress import (
    PublicV40TierScenario, assess_public_v40_tier_stress,
)
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)

_PATH = "/api/v1/contract/detail/country"
_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")


def shareable(report: PublicV40TierScenario) -> dict[str, Any]:
    return {
        "status": report.status,
        "risk_limit_mode": report.risk_limit_mode,
        "risk_limit_type": report.risk_limit_type,
        "public_contract_eligible": report.public_contract_eligible,
        "published_tiers_valid": report.published_tiers_valid,
        "published_tier_count": report.published_tier_count,
        "candidate_in_published_tier": report.candidate_in_published_tier,
        "published_research_mmr_observed":
            report.public_research_maintenance_rate is not None,
        "reference_notional_research_computed":
            report.entry_reference_notional_usdt is not None,
        "stop_gap_notional_multiple": str(report.stop_plus_gap_notional_multiple),
        "public_maintenance_reserve_research_computed":
            report.research_published_maintenance_reserve_usdt is not None,
        "public_liquidation_fee_reserve_research_computed":
            report.research_published_liquidation_fee_reserve_usdt is not None,
        "public_tiers_are_future_upper_bound": False,
        "private_current_cross_verified": False,
        "account_specific_api_order_access_verified": False,
        "future_stressed_tiers_certified": False,
        "account_collateral_certified": False,
        "native_stop_protection_verified": False,
        "venue_liquidation_certified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }


async def inspect_public_tier_scenario(
    public: Any, symbol: str, *, contracts: str,
    entry_reference_price: str, leverage: int,
) -> dict[str, Any]:
    def blocked(status: str) -> dict[str, Any]:
        return shareable(PublicV40TierScenario(status))

    if (type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None
        or symbol in {"BTC_USDT", "ETH_USDT"}):
        return blocked("PUBLIC_V40_SYMBOL_UNVERIFIED")
    if public is None or not callable(getattr(public, "get", None)):
        return blocked("PUBLIC_V40_CLIENT_UNVERIFIED")
    rows = []
    for _ in range(2):
        try:
            response = await public.get(_PATH, params={"symbol": symbol})
            response.raise_for_status()
            envelope = response.json()
        except Exception:
            return blocked("PUBLIC_V40_GET_FAILED")
        if (type(envelope) is not dict or envelope.get("success") is not True
            or type(envelope.get("code")) is not int or envelope["code"] != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol):
            return blocked("PUBLIC_V40_RESPONSE_UNVERIFIED")
        rows.append(envelope["data"])
    try:
        canonical = [
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for row in rows
        ]
    except (ValueError, TypeError):
        return blocked("PUBLIC_V40_RESPONSE_UNVERIFIED")
    if canonical[0] != canonical[1]:
        return blocked("PUBLIC_V40_METADATA_DRIFT")

    result = assess_public_v40_tier_stress(
        rows[1], contracts=contracts,
        entry_reference_price=entry_reference_price, leverage=leverage,
    )
    return shareable(result)


async def run(symbol: str, *, contracts: str,
              entry_reference_price: str, leverage: int = 1,
              environ: Mapping[str, str] | None = None,
              public: Any = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return shareable(PublicV40TierScenario("PUBLIC_V40_PAPER_KILL_REQUIRED"))
    if public is not None:
        return await inspect_public_tier_scenario(
            public, symbol, contracts=contracts,
            entry_reference_price=entry_reference_price, leverage=leverage,
        )
    try:
        import httpx
    except ImportError:
        return shareable(PublicV40TierScenario("PUBLIC_V40_HTTPX_MISSING"))
    try:
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            timeout=15.0, follow_redirects=False,
            headers={"User-Agent": "T100-public-v40-readonly-research/1.0"},
        ) as client:
            return await asyncio.wait_for(
                inspect_public_tier_scenario(
                    client, symbol, contracts=contracts,
                    entry_reference_price=entry_reference_price,
                    leverage=leverage,
                ), timeout=35.0,
            )
    except asyncio.TimeoutError:
        return shareable(PublicV40TierScenario("PUBLIC_V40_TIMED_OUT"))
    except Exception:
        return shareable(PublicV40TierScenario("PUBLIC_V40_READ_FAILED"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public-only V40 BY_VOLUME stress research; no account reads",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--contracts", required=True)
    parser.add_argument("--entry-reference-price", required=True)
    parser.add_argument("--leverage", type=int, default=1)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(
            args.symbol, contracts=args.contracts,
            entry_reference_price=args.entry_reference_price,
            leverage=args.leverage,
        ))
    except Exception:
        result = shareable(PublicV40TierScenario("PUBLIC_V40_READ_FAILED"))
    print(json.dumps(result, sort_keys=True, indent=2))
    print("PUBLIC GET ONLY. REFERENCE INPUTS ARE RESEARCH ASSUMPTIONS, NOT FILLS.")
    print("FUTURE TIERS, ACCOUNT COLLATERAL AND LIVE AUTHORIZATION REMAIN UNVERIFIED.")
    if result["status"] != "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
