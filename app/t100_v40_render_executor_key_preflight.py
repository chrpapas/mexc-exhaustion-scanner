"""T100 V40 *trading-key* Render first-stage signed-GET-only proof.

The operator created a Futures trading-enabled key for the dedicated WEB
pilot subaccount. This CLI validates it in a separate **currently suspended**
Render trader-service build, as a ONE-OFF JOB, without ever submitting any
order or starting a live executor.

No scanner-owner database principal, stop mutation, MEXC POST, DB, Discord,
SQLite or strategy state touched. It is NOT the live V40 adapter. In
particular, an API key's ability to GET inventory DOES NOT certify server
side trading rights, identity, country eligibility or SHORT CROSS safety.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass
from decimal import Decimal as D
from typing import Any, Awaitable, Callable, Mapping

from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult, observe_live_pilot


@dataclass(frozen=True)
class V40TradeKeyGetOnlyPreflight:
    status: str
    signed_get_bookends_complete: bool = False
    hedge_mode_observed: bool = False
    current_flat_equity_verified: bool = False
    account_equity_usdt: str | None = None
    available_balance_usdt: str | None = None
    research_only_notional_ceiling_usdt: str | None = None
    exact_key_fingerprint_pinned: bool = False
    subaccount_identity_independently_verified: bool = False
    exchange_side_trade_permission_verified: bool = False
    current_market_contract_risk_verified: bool = False
    fresh_v40_signal_verified: bool = False
    cross_margin_verified: bool = False
    native_stop_loss_verified: bool = False
    order_adapter_certified: bool = False
    independent_nonflat_monitor_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0
    discord_requests: int = 0


def safe_env(env: Mapping[str, str]) -> bool:
    """Forbidden if older paper trader, real execution or DB env can run."""
    return all((
        env.get("T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED") == "true",
        env.get("T100_V40_ACCOUNT_PROFILE") == "DEDICATED_WEB_PILOT",
        env.get("T100_EXECUTION_MODE") == "paper",
        env.get("T100_LIVE_ORDER_API_ENABLED") == "false",
        env.get("T100_LIVE_KILL_SWITCH") == "true",
        env.get("EXECUTION_ENABLED") == "false",
        env.get("TRADING_MODE") == "paper",
        not env.get("DATABASE_URL"),
        not env.get("MEXC_API_KEY"),
        not env.get("MEXC_API_SECRET"),
    ))


async def one_get_only(
    env: Mapping[str, str],
    *,
    observe: Callable[..., Awaitable[PilotGetOnlyResult]] = observe_live_pilot,
) -> V40TradeKeyGetOnlyPreflight:
    def halted(code: str) -> V40TradeKeyGetOnlyPreflight:
        return V40TradeKeyGetOnlyPreflight(status=code)

    if not safe_env(env):
        return halted("V40_EXECUTOR_PREFLIGHT_PAPER_OR_ISOLATION_HALT")
    key = env.get("T100_V40_TRADER_API_KEY", "")
    secret = env.get("T100_V40_TRADER_API_SECRET", "")
    expected_hash = env.get("T100_V40_TRADER_API_KEY_SHA256", "")
    if (
        type(key) is not str or len(key) < 12
        or type(secret) is not str or len(secret) < 16
        or type(expected_hash) is not str
        or re.fullmatch(r"[a-f0-9]{64}", expected_hash) is None
        or not hmac.compare_digest(
            expected_hash, hashlib.sha256(key.encode()).hexdigest()
        )
    ):
        return halted("V40_EXECUTOR_PREFLIGHT_EXACT_TRADING_KEY_PIN_HALT")
    try:
        observed = await asyncio.wait_for(
            observe(key=key, secret=secret), timeout=25,
        )
        if (
            type(observed) is not PilotGetOnlyResult
            or observed.status != "PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
            or observed.samples != 2
            or observed.position_count != 0
            or observed.native_stop_count != 0
            or observed.hedge_mode_observed is not True
            or observed.inventory_bookends_equal is not True
            or observed.mexc_posts != 0
            or observed.db_mutations != 0
            or observed.live_authorization is not False
        ):
            return halted("V40_EXECUTOR_PREFLIGHT_INVENTORY_NOT_FLAT_OR_UNVERIFIED")
        equity = D(observed.account_equity_usdt)
        available = D(observed.available_balance_usdt)
        if (
            not equity.is_finite() or equity <= 0
            or not available.is_finite() or available <= 0
            or available > equity + D("0.00000001")
        ):
            return halted("V40_EXECUTOR_PREFLIGHT_USDT_ECONOMICS_UNVERIFIED")
        research_ceiling = min(D("12.50"),D("0.25")*min(equity,available))
        if research_ceiling <= 0:
            return halted("V40_EXECUTOR_PREFLIGHT_ZERO_ECONOMIC_BUDGET")
        return V40TradeKeyGetOnlyPreflight(
            status="V40_TRADING_KEY_SIGNED_GET_FLAT_VERIFIED_NONAUTHORIZING",
            signed_get_bookends_complete=True,
            hedge_mode_observed=True,
            current_flat_equity_verified=True,
            account_equity_usdt=str(equity),
            available_balance_usdt=str(available),
            research_only_notional_ceiling_usdt=str(
                research_ceiling.quantize(D("0.00000001"))
            ),
            exact_key_fingerprint_pinned=True,
        )
    except Exception:
        return halted("V40_EXECUTOR_PREFLIGHT_SIGNED_GET_FAILED")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--once", action="store_true", required=True)
    p.parse_args()
    try:
        r = asyncio.run(one_get_only(os.environ))
    except Exception:
        r = V40TradeKeyGetOnlyPreflight("V40_EXECUTOR_PREFLIGHT_UNEXPECTED_HALT")
    print(json.dumps(asdict(r),sort_keys=True))
    print("MEXC_SIGNED_GET_ONLY=true MEXC_POSTS=0 DB_WRITES=0 LIVE_AUTHORIZATION=false")
    return 0 if r.signed_get_bookends_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
