"""Guarded GET-only MEXC current regular limit-order scope check.

Eight signed GETs max across two rounds: candidate positions, account
order counts (before and after) and the paginated current regular order list.
Requires explicit paper mode, live API disabled, kill switch engaged, local
MEXC credential environment. Never submits/cancels orders or changes margin.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_candidate_limit_scope import (
    CandidateLimitOrderWitness, inspect_candidate_limit_orders,
)
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)


async def run(symbol: str, *,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(CandidateLimitOrderWitness("CANDIDATE_LIMIT_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(CandidateLimitOrderWitness("CANDIDATE_LIMIT_CREDENTIALS_MISSING"))

    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    client = None
    try:
        client = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        result = await asyncio.wait_for(
            inspect_candidate_limit_orders(ReadOnlyMexcProbe(client), symbol),
            timeout=35.0,
        )
        return asdict(result)
    except asyncio.TimeoutError:
        return asdict(CandidateLimitOrderWitness("CANDIDATE_LIMIT_TIMED_OUT"))
    except Exception:
        return asdict(CandidateLimitOrderWitness("CANDIDATE_LIMIT_READ_FAILED"))
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nonauthorizing two-window current open limit order symbol scope",
    )
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.symbol))
    except Exception:
        result = asdict(CandidateLimitOrderWitness("CANDIDATE_LIMIT_READ_FAILED"))
    print(json.dumps(result, sort_keys=True, indent=2))
    print("GET-ONLY; NO TRADES/ORDER CANCEL; NO MARGIN CHANGES; LIVE DISABLED.")
    if result["status"] != (
        "CANDIDATE_LIMIT_ONLY_OTHER_CONTRACT_OBSERVED_NONAUTHORIZING"
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
