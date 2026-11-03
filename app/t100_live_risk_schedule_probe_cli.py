"""Manual opt-in public risk schedule shape inspection using current open positions.

No PnL validation dependency, but also no private tier certification or order
authorization. Paper mode, order API disabled and kill switch engaged are
required BEFORE network client construction. Local credentials only.
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
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_risk_schedule_probe import (
    RiskScheduleProbeReport, inspect_risk_schedule_shape,
)


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(RiskScheduleProbeReport(
            status="RISK_SHAPE_PAPER_KILL_REQUIRED",
        ))
    key, secret = env.get("MEXC_API_KEY", ""), env.get("MEXC_API_SECRET", "")
    if not key or not secret:
        return asdict(RiskScheduleProbeReport(
            status="RISK_SHAPE_CREDENTIALS_MISSING",
        ))
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        observation = await asyncio.wait_for(
            inspect_risk_schedule_shape(
                ReadOnlyMexcProbe(client), client.client,
            ),
            timeout=25.0,
        )
        return asdict(observation)
    except asyncio.TimeoutError:
        return asdict(RiskScheduleProbeReport(
            status="RISK_SHAPE_TIMED_OUT",
        ))
    except Exception:
        return asdict(RiskScheduleProbeReport(
            status="RISK_SHAPE_DIAGNOSTIC_FAILED",
        ))
    finally:
        await client.close()


def main() -> None:
    try:
        result = asyncio.run(run())
    except Exception:
        result = asdict(RiskScheduleProbeReport(
            status="RISK_SHAPE_DIAGNOSTIC_FAILED",
        ))
    print(json.dumps(result, sort_keys=True, indent=2))
    print("PRIVATE/PUBLIC GET ONLY. NO ORDERS. LIVE TRADING DISABLED.")
    if result["status"] != "RISK_SHAPE_PUBLIC_METADATA_OBSERVED_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
