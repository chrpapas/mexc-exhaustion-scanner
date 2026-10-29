"""Operator-invoked public-only MEXC country contract API eligibility census.

No credentials are read. No private API, POST, orders, DB, or Render.
Only fixed aggregate counts are returned. MEXC country endpoint data
may depend on region and is NOT account-level API order certification.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_api_eligibility_census import (
    CountryApiCensus, inspect_country_api_census,
)
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(CountryApiCensus(
            status="COUNTRY_API_CENSUS_PAPER_KILL_REQUIRED",
        ))
    # No MEXC key or secret is read, loaded or put in HTTP request headers.
    import httpx
    try:
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "T100-public-readonly-eligibility-census/1.0"},
            follow_redirects=False,
        ) as http_client:
            result = await asyncio.wait_for(
                inspect_country_api_census(http_client),
                timeout=45.0,
            )
            return asdict(result)
    except asyncio.TimeoutError:
        return asdict(CountryApiCensus(status="COUNTRY_API_CENSUS_TIMED_OUT"))
    except Exception:
        return asdict(CountryApiCensus(status="COUNTRY_API_CENSUS_READ_FAILED"))


def main() -> None:
    try:
        response = asyncio.run(run())
    except Exception:
        response = asdict(CountryApiCensus(
            status="COUNTRY_API_CENSUS_READ_FAILED",
        ))
    print(json.dumps(response, sort_keys=True, indent=2))
    print("TWO PUBLIC GETS MAX. NO CREDENTIALS. NO ORDERS. LIVE DISABLED.")
    if response["status"] != (
        "COUNTRY_API_CENSUS_CURRENT_PUBLIC_COUNTS_NONAUTHORIZING"
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
