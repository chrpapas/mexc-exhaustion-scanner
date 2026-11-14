"""Render isolated pilot GET-only *candidate contract* capability witness.

Only uses MEXC_API_KEY and MEXC_API_SECRET, the same two keys as the
successful flat account preflight. Never sends POST, reads/writes DB,
changes leverage or margin, touches BOXWAVE, or authorizes live orders.

Run from the isolated V40 disabled trader's Render shell, after passing
the flat/hedge two-bookend account preflight. An explicit symbol is
required. Passing public contract + private tier checks alone is NOT a
real V40 signal, a guaranteed order permission or native-stop proof.

This calls the already-tested Mac-capability observer, with credentials
injected directly from the Render environment rather than Keychain.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
import re
from typing import Awaitable, Callable, Mapping

from app.t100_v40_render_executor_key_preflight import safe_env
from scripts.t100_v40_pilot_capability_get_only import (
    PilotCapabilityReport,
    observe_pilot_capability,
)


@dataclass(frozen=True)
class RenderCapabilityOutcome:
    status: str
    symbol: str = "UNVERIFIED"
    flat_hedge_account_bookended: bool = False
    public_futures_contract_eligible_observed: bool = False
    current_private_short_cross_1x_tier_observed: bool = False
    private_capability_drift_source: str = "NONE"
    current_short_tier_supports_one_contract: bool = False
    short_leverage_observed: int | None = None
    api_key_futures_write_permission_verified: bool = False
    exact_subaccount_identity_independently_verified: bool = False
    cross_collateral_verified: bool = False
    native_market_stop_certified: bool = False
    v40_fresh_signal_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


async def assess_render_capability(
    env: Mapping[str,str], *,
    symbol: str,
    observe: Callable[...,Awaitable[PilotCapabilityReport]] = observe_pilot_capability,
) -> RenderCapabilityOutcome:
    def halt(status: str) -> RenderCapabilityOutcome:
        return RenderCapabilityOutcome(status=status)
    if not safe_env(env):
        return halt("V40_RENDER_CAPABILITY_ISOLATION_OR_KILL_HALT")
    key, secret = env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12
        or type(secret) is not str or len(secret)<16
        or key!=key.strip() or secret!=secret.strip()
    ):
        return halt("V40_RENDER_CAPABILITY_MEXC_CREDENTIALS_INVALID_HALT")
    if (
        type(symbol) is not str
        or not re.fullmatch(r"[A-Z0-9]+_USDT",symbol)
    ):
        return halt("V40_RENDER_CAPABILITY_SYMBOL_INVALID_HALT")
    try:
        result=await asyncio.wait_for(
            observe(key=key,secret=secret,symbol=symbol),timeout=65,
        )
        if (
            type(result) is not PilotCapabilityReport
            or result.symbol!=symbol
            or result.mexc_posts!=0 or result.db_mutations!=0
            or result.live_authorization is not False
            or result.entry_allowed is not False
        ):
            return halt("V40_RENDER_CAPABILITY_REPORT_INVALID_HALT")
        return RenderCapabilityOutcome(
            status=result.status,
            symbol=result.symbol,
            flat_hedge_account_bookended=result.flat_hedge_account_bookended,
            public_futures_contract_eligible_observed=result.public_futures_contract_eligible_observed,
            current_private_short_cross_1x_tier_observed=result.current_private_short_cross_1x_tier_observed,
            private_capability_drift_source=result.private_capability_drift_source,
            current_short_tier_supports_one_contract=result.current_short_tier_supports_one_contract,
            short_leverage_observed=result.short_leverage_observed,
        )
    except Exception:
        return halt("V40_RENDER_CAPABILITY_SIGNED_GET_FAILED_HALT")


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol",required=True,
                   help="Exact MEXC contract, e.g. BTC_USDT (research diagnostic only)")
    args=p.parse_args()
    try:
        result=asyncio.run(
            assess_render_capability(os.environ,symbol=args.symbol)
        )
    except Exception:
        result=RenderCapabilityOutcome("V40_RENDER_CAPABILITY_UNEXPECTED_HALT")
    print(json.dumps(asdict(result),sort_keys=True))
    print("MEXC_GET_ONLY=true MEXC_POSTS=0 DATABASE_WRITES=0 LIVE_AUTHORIZATION=false")
    return 0 if (
        result.flat_hedge_account_bookended
        and result.public_futures_contract_eligible_observed
        and result.current_private_short_cross_1x_tier_observed
        and result.current_short_tier_supports_one_contract
    ) else 2


if __name__=="__main__":
    raise SystemExit(main())
