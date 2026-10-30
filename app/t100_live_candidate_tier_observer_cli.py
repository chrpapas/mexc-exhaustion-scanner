"""Manual read-only comparison of one T100 SHORT candidate's CURRENT risk tier.

Exactly two public and (if eligible) two private scoped GETs. Uses locally
configured MEXC credentials only. No POST, orders, stops, DB or Render calls.
The --contracts argument is a research proposal, NOT V40 sizing certification.

Example:
 T100_EXECUTION_MODE=paper T100_LIVE_ORDER_API_ENABLED=false \\
 T100_LIVE_KILL_SWITCH=true python3 -m app.t100_live_candidate_tier_observer_cli \\
 --symbol SIREN_USDT --contracts 1
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_candidate_tier_observer import (
    CandidateTierObservation, inspect_candidate_tier,
)
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)


async def run(symbol: str, contracts: str, *,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(CandidateTierObservation("CANDIDATE_TIER_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(CandidateTierObservation("CANDIDATE_TIER_CREDENTIALS_MISSING"))

    # Importing a read-only facade and initializing the signed client only
    # after the explicit paper/kill controls and credential checks pass.
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    client = None
    try:
        client = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        report = await asyncio.wait_for(
            inspect_candidate_tier(
                ReadOnlyMexcProbe(client), client.client, symbol,
                proposed_contracts=contracts,
            ),
            timeout=30.0,
        )
        return asdict(report)
    except asyncio.TimeoutError:
        return asdict(CandidateTierObservation("CANDIDATE_TIER_TIMED_OUT"))
    except Exception:
        return asdict(CandidateTierObservation("CANDIDATE_TIER_READ_FAILED"))
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only candidate CURRENT tier parity; no orders"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--contracts", required=True)
    args = parser.parse_args()
    try:
        output = asyncio.run(run(args.symbol, args.contracts))
    except Exception:
        output = asdict(CandidateTierObservation("CANDIDATE_TIER_READ_FAILED"))
    print(json.dumps(output, indent=2, sort_keys=True))
    print("CURRENT ONLY. NO ORDERS. LIVE DISABLED. NOT V40 RISK CERTIFICATION.")
    if output["status"] != "CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
