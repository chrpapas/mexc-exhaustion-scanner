"""Manual T100 public-only MEXC country tier-shape audit (no private access).

Run only with T100_EXECUTION_MODE=paper, T100_LIVE_ORDER_API_ENABLED=false,
T100_LIVE_KILL_SWITCH=true. Exactly two public contract GETs, no credentials,
no orders or changes to Render/database/strategy/trading.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_country_tier_quality import (
    CountryTierQuality, inspect_country_tier_quality,
)
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(CountryTierQuality(
            status="COUNTRY_TIER_QUALITY_PAPER_KILL_REQUIRED",
        ))

    import httpx
    try:
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "T100-public-readonly-tier-quality/1.0"},
            follow_redirects=False,
        ) as public:
            outcome = await asyncio.wait_for(
                inspect_country_tier_quality(public), timeout=45.0,
            )
            return asdict(outcome)
    except asyncio.TimeoutError:
        return asdict(CountryTierQuality(status="COUNTRY_TIER_QUALITY_TIMED_OUT"))
    except Exception:
        return asdict(CountryTierQuality(status="COUNTRY_TIER_QUALITY_READ_FAILED"))


def main() -> None:
    try:
        report = asyncio.run(run())
    except Exception:
        report = asdict(CountryTierQuality(status="COUNTRY_TIER_QUALITY_READ_FAILED"))
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PUBLIC TWO GETS MAX. NO KEYS, NO ORDERS, NO AUTHORIZATION.")
    if report["status"] != "COUNTRY_TIER_QUALITY_CURRENT_PUBLIC_SHAPES_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
