"""Manual GET-only nonauthorizing MEXC unrealized-PnL synchronization test.

Paper + kill-switch controls are mandatory before credentials or HTTP client
construction. Three sequential position/assets/position samples maximum.
Never use this as a live trading, liquidation or collateral authorization.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import time
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_pnl_sync_observation import (
    PnlSyncReport, observe_pnl_sync,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(PnlSyncReport(status="PNL_SYNC_PAPER_KILL_REQUIRED"))
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return asdict(PnlSyncReport(status="PNL_SYNC_CREDENTIALS_MISSING"))

    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        witness = await asyncio.wait_for(
            observe_pnl_sync(
                ReadOnlyMexcProbe(client),
                monotonic_ms=lambda: int(time.monotonic() * 1000),
            ),
            timeout=25.0,
        )
        return asdict(witness)
    except asyncio.TimeoutError:
        return asdict(PnlSyncReport(status="PNL_SYNC_DIAGNOSTIC_TIMED_OUT"))
    except Exception:
        return asdict(PnlSyncReport(status="PNL_SYNC_DIAGNOSTIC_FAILED"))
    finally:
        await client.close()


def main() -> None:
    try:
        report = asyncio.run(run())
    except Exception:
        report = asdict(PnlSyncReport(status="PNL_SYNC_DIAGNOSTIC_FAILED"))
    print(json.dumps(report, sort_keys=True, indent=2))
    print("AT MOST NINE PRIVATE GET REQUESTS. NO ORDERS. LIVE TRADING DISABLED.")
    if report["status"] in {
        "PNL_SYNC_CREDENTIALS_MISSING", "PNL_SYNC_PAPER_KILL_REQUIRED",
        "PNL_SYNC_READ_OR_SCHEMA_UNVERIFIED", "PNL_SYNC_STALE_READ_WINDOW",
        "PNL_SYNC_DIAGNOSTIC_TIMED_OUT", "PNL_SYNC_DIAGNOSTIC_FAILED",
        "PNL_SYNC_INVALID_CONFIG",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
