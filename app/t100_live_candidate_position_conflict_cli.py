"""Operator-invoked nonauthorizing SIREN candidate conflict check.

Requires paper mode, live API off, kill engaged, local MEXC credentials.
Exactly four signed GETs: two scoped positions and two account order counts.
No trades, margin mode changes, stop mutation, DB/Render side effects.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_candidate_position_conflict import (
    CandidatePositionConflict, inspect_candidate_position_conflicts,
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
        return asdict(CandidatePositionConflict("CANDIDATE_POSITION_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(CandidatePositionConflict("CANDIDATE_POSITION_CREDENTIALS_MISSING"))

    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    client = None
    try:
        client = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        result = await asyncio.wait_for(
            inspect_candidate_position_conflicts(
                ReadOnlyMexcProbe(client), symbol,
            ),
            timeout=30,
        )
        return asdict(result)
    except asyncio.TimeoutError:
        return asdict(CandidatePositionConflict("CANDIDATE_POSITION_TIMED_OUT"))
    except Exception:
        return asdict(CandidatePositionConflict("CANDIDATE_POSITION_READ_FAILED"))
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Current SIREN candidate open-position/order conflict check (GET only)",
    )
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.symbol))
    except Exception:
        report = asdict(CandidatePositionConflict("CANDIDATE_POSITION_READ_FAILED"))
    print(json.dumps(report, sort_keys=True, indent=2))
    print("FOUR PRIVATE GETS MAX; NO ORDERS; NO MARGIN CHANGES; LIVE DISABLED.")
    if report["status"] not in {
        "CANDIDATE_EXISTING_POSITION_PRESENT_NONAUTHORIZING",
        "CANDIDATE_NO_POSITION_ACCOUNT_ORDERS_PRESENT_UNSCOPED",
        "CANDIDATE_NO_POSITION_ACCOUNT_ORDER_FREE_OBSERVED_NONAUTHORIZING",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
