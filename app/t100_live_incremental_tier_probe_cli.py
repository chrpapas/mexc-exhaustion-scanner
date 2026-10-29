"""Opt-in standalone GET-only public incremental-tier / private-MMR research.

Under no circumstances is the returned candidate a trading permission.
Paper, disabled live order API and kill switch are mandatory BEFORE any HTTP.
No secrets, symbols, prices, quantities, risk rates or raw errors are printed.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_incremental_tier_probe import (
    IncrementalTierProbeReport, inspect_incremental_tier_candidate,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(IncrementalTierProbeReport(
            status="INCR_PROBE_PAPER_KILL_REQUIRED",
        ))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(IncrementalTierProbeReport(
            status="INCR_PROBE_CREDENTIALS_MISSING",
        ))

    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        witness = await asyncio.wait_for(
            inspect_incremental_tier_candidate(
                ReadOnlyMexcProbe(client), client.client,
            ),
            timeout=25.0,
        )
        return asdict(witness)
    except asyncio.TimeoutError:
        return asdict(IncrementalTierProbeReport(
            status="INCR_PROBE_TIMED_OUT",
        ))
    except Exception:
        return asdict(IncrementalTierProbeReport(
            status="INCR_PROBE_DIAGNOSTIC_FAILED",
        ))
    finally:
        await client.close()


def main() -> None:
    try:
        report = asyncio.run(run())
    except Exception:
        report = asdict(IncrementalTierProbeReport(
            status="INCR_PROBE_DIAGNOSTIC_FAILED",
        ))
    print(json.dumps(report, sort_keys=True, indent=2))
    print("PRIVATE/PUBLIC GET ONLY. RESEARCH CANDIDATE. NO ORDERS. LIVE DISABLED.")
    if report["status"] not in {
        "INCR_PROBE_CURRENT_PRIVATE_BELOW_CANDIDATE_NONAUTHORIZING",
        "INCR_PROBE_PUBLIC_CANDIDATE_PRIVATE_COMPARED_NONAUTHORIZING",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
