"""Operator-invoked SIREN open-TP/SL scope witness (read-only).

Run with paper/kill controls and existing local MEXC keys. At most eight
signed GETs in two rounds: scoped positions, account order counts, global
current TP/SL, symbol-scoped TP/SL. Never changes or cancels manual orders.
No trading POST, DB, Render, or margin/leverage configuration changes.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_candidate_stop_scope import (
    CandidateStopScope, inspect_candidate_stop_scope,
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
        return asdict(CandidateStopScope("CANDIDATE_STOP_SCOPE_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(CandidateStopScope("CANDIDATE_STOP_SCOPE_CREDENTIALS_MISSING"))

    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    client = None
    try:
        client = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        outcome = await asyncio.wait_for(
            inspect_candidate_stop_scope(ReadOnlyMexcProbe(client), symbol),
            timeout=35.0,
        )
        return asdict(outcome)
    except asyncio.TimeoutError:
        return asdict(CandidateStopScope("CANDIDATE_STOP_SCOPE_TIMED_OUT"))
    except Exception:
        return asdict(CandidateStopScope("CANDIDATE_STOP_SCOPE_READ_FAILED"))
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GET-only current candidate TP/SL order scope, no mutations"
    )
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    try:
        outcome = asyncio.run(run(args.symbol))
    except Exception:
        outcome = asdict(CandidateStopScope("CANDIDATE_STOP_SCOPE_READ_FAILED"))
    print(json.dumps(outcome, indent=2, sort_keys=True))
    print("EIGHT PRIVATE GETS MAX; NO ORDERS; NO MARGIN CHANGES; LIVE DISABLED.")
    if outcome["status"] != (
        "CANDIDATE_STOP_SCOPE_NO_POSITION_NO_TPSL_OBSERVED_NONAUTHORIZING"
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
