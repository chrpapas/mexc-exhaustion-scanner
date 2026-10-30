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


def _safe_failure_status(exc: Exception) -> str:
    """Classify exceptions without returning URLs, tokens, payloads or symbols."""
    if isinstance(exc, (ArithmeticError, ValueError, TypeError, KeyError)):
        return "COUNTRY_TIER_QUALITY_DATA_PROCESSING_FAILED"
    if isinstance(exc, (ConnectionError, OSError)):
        return "COUNTRY_TIER_QUALITY_LOCAL_TRANSPORT_FAILED"
    return "COUNTRY_TIER_QUALITY_UNEXPECTED_RUNTIME_FAILED"


async def run(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return asdict(CountryTierQuality(
            status="COUNTRY_TIER_QUALITY_PAPER_KILL_REQUIRED",
        ))

    try:
        import httpx
    except ImportError:
        return asdict(CountryTierQuality(
            status="COUNTRY_TIER_QUALITY_HTTPX_MISSING",
        ))
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
    except Exception as exc:
        return asdict(CountryTierQuality(status=_safe_failure_status(exc)))


def main() -> None:
    try:
        report = asyncio.run(run())
    except Exception as exc:
        report = asdict(CountryTierQuality(status=_safe_failure_status(exc)))
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PUBLIC TWO GETS MAX. NO KEYS, NO ORDERS, NO AUTHORIZATION.")
    if report["status"] != "COUNTRY_TIER_QUALITY_CURRENT_PUBLIC_SHAPES_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
