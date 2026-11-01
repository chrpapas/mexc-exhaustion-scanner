"""Opt-in read-only MEXC current short-side margin-mode corroboration.

Paper + kill required. Uses existing locally configured credentials. Four
signed GETs max: risk_limit twice, position/leverage twice. No POST/orders,
change-leverage, account mutation, DB or Render. Output is status-only.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_margin_mode_witness import (
    MarginModeWitness, inspect_margin_mode,
)


async def run(symbol: str, *,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(MarginModeWitness("MARGIN_MODE_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(MarginModeWitness("MARGIN_MODE_CREDENTIALS_MISSING"))

    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    client = None
    try:
        client = MexcTradeClient(
            "https://api.mexc.com", api_key=key, api_secret=secret,
        )
        result = await asyncio.wait_for(
            inspect_margin_mode(ReadOnlyMexcProbe(client), client, symbol),
            timeout=30.0,
        )
        return asdict(result)
    except asyncio.TimeoutError:
        return asdict(MarginModeWitness("MARGIN_MODE_TIMED_OUT"))
    except Exception:
        return asdict(MarginModeWitness("MARGIN_MODE_READ_FAILED"))
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GET-only candidate isolated/cross mode corroboration",
    )
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.symbol))
    except Exception:
        result = asdict(MarginModeWitness("MARGIN_MODE_READ_FAILED"))
    print(json.dumps(result, sort_keys=True, indent=2))
    print("FOUR PRIVATE GETS MAX. NO ORDERS. LIVE DISABLED.")
    if result["status"] not in {
        "MARGIN_MODE_CURRENT_ISOLATED_CORROBORATED_NONAUTHORIZING",
        "MARGIN_MODE_CURRENT_CROSS_CORROBORATED_NONAUTHORIZING",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
