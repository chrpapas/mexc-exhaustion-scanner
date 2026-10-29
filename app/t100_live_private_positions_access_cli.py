"""Manual opt-in access-only MEXC diagnostic: one signed private GET.

Usage from trusted Mac shell with existing MEXC_API_KEY and MEXC_API_SECRET:
T100_EXECUTION_MODE=paper T100_LIVE_ORDER_API_ENABLED=false \
T100_LIVE_KILL_SWITCH=true python3 -m app.t100_live_private_positions_access_cli

Output is limited to fixed error categories and allowlisted HTTP status.
No credentials, symbols, position values, URLs or raw exchange errors.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_private_positions_access import (
    _report, probe_private_positions_access,
)


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _report("DIAGNOSTIC_PAPER_KILL_REQUIRED")
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    if not key or not secret:
        return _report("DIAGNOSTIC_CREDENTIALS_MISSING")

    # The MEXC client is constructed only AFTER all safety guards pass.
    # Reuse the exact existing signing implementation and endpoint.
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        return await asyncio.wait_for(
            probe_private_positions_access(
                http_client=client.client,
                signed_headers=client._auth_headers,
                clock_ms=lambda: int(time.time() * 1000),
            ),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        return _report("NETWORK_TIMEOUT")
    except Exception:
        return _report("DIAGNOSTIC_RUNTIME_FAILED")
    finally:
        await client.close()


def main() -> None:
    try:
        report = asyncio.run(run())
    except Exception:
        report = _report("DIAGNOSTIC_RUNTIME_FAILED")
    print(json.dumps(report, sort_keys=True, indent=2))
    print("ONE PRIVATE GET ONLY. NO ORDERS. LIVE TRADING DISABLED.")
    if report["status"] != "POSITIONS_READ_SUCCEEDED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
