"""Read-only candidate-specific MEXC contract preflight.

Never adopts or mutates existing positions. Manual positions are not inspected.
This reports metadata evidence only: NOT live authorization or margin certification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
import json
import os
import re
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_incremental_tier_candidate import classify_incremental_eligibility
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_risk_schedule_shape import classify_risk_schedule_shapes

COUNTRY = "/api/v1/contract/detail/country"
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]+_USDT")


@dataclass(frozen=True)
class CandidatePreflight:
    status: str
    contract_state: str = "NOT_CHECKED"
    api_order_access: str = "NOT_CHECKED"
    cross_margin_support: str = "NOT_CHECKED"
    contract_duration: str = "NOT_CHECKED"
    quote_currency: str = "NOT_CHECKED"
    settlement_currency: str = "NOT_CHECKED"
    risk_schedule_mode: str = "NOT_CHECKED"
    risk_schedule_type: str = "NOT_CHECKED"
    risk_schedule_fields: str = "NOT_CHECKED"
    public_metadata_two_get_consistent: bool = False
    private_short_tier_observed: bool = False
    private_risk_scope_verified: bool = False
    manual_positions_ignored: bool = True
    account_collateral_certified: bool = False
    future_stressed_tiers_certified: bool = False
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _static(row: dict[str, Any]) -> str:
    keys = (
        "symbol", "state", "apiAllowed", "positionOpenType", "futureType",
        "quoteCoin", "settleCoin", "riskLimitMode", "riskLimitType",
        "riskLongShortSwitch", "riskLevelLimit", "riskBaseVol",
        "riskIncrVol", "riskBaseVolShort", "riskIncrVolShort",
        "riskIncrMmr", "riskIncrImr", "maintenanceMarginRate",
        "initialMarginRate", "riskLimitCustom", "contractSize",
    )
    return json.dumps({k: row.get(k) for k in keys}, sort_keys=True, allow_nan=False)


async def inspect_candidate(private: ReadOnlyMexcProbe, public: Any, symbol: str) -> CandidatePreflight:
    if type(symbol) is not str or not SYMBOL_PATTERN.fullmatch(symbol):
        return CandidatePreflight("INVALID_CANDIDATE_SYMBOL")
    if not isinstance(private, ReadOnlyMexcProbe) or public is None:
        return CandidatePreflight("INVALID_PROBE_CONFIGURATION")
    rows = []
    for _ in range(2):
        try:
            response = await public.get(COUNTRY, params={"symbol": symbol})
            response.raise_for_status()
            payload = response.json()
            if (type(payload) is not dict or payload.get("success") is not True
                    or type(payload.get("code")) is not int or payload["code"] != 0
                    or type(payload.get("data")) is not dict
                    or payload["data"].get("symbol") != symbol):
                return CandidatePreflight("PUBLIC_SCHEMA_UNVERIFIED")
            rows.append(payload["data"])
        except Exception:
            return CandidatePreflight("PUBLIC_GET_FAILED")
    try:
        if _static(rows[0]) != _static(rows[1]):
            return CandidatePreflight("PUBLIC_METADATA_DRIFT")
    except (TypeError, ValueError):
        return CandidatePreflight("PUBLIC_SCHEMA_UNVERIFIED")

    eligibility = classify_incremental_eligibility(rows[0])
    shape = classify_risk_schedule_shapes({symbol: rows[0]})
    shared = dict(
        contract_state=eligibility.state,
        api_order_access=eligibility.api_order_access,
        cross_margin_support=eligibility.cross_margin_support,
        contract_duration=eligibility.contract_duration,
        quote_currency=eligibility.quote_currency,
        settlement_currency=eligibility.settlement_currency,
        risk_schedule_mode=shape.mode,
        risk_schedule_type=shape.limit_type,
        risk_schedule_fields=shape.schedule_fields,
        public_metadata_two_get_consistent=True,
    )
    if not eligibility.contract_eligible:
        return CandidatePreflight("CONTRACT_NOT_ELIGIBLE", **shared)
    if shape.mode not in {"CUSTOM", "INCREASE"} or shape.limit_type != "BY_VOLUME":
        return CandidatePreflight("RISK_SCHEDULE_UNSUPPORTED", **shared)
    if shape.schedule_fields not in {
        "CUSTOM_FIELDS_PRESENT", "INCREMENTAL_FIELDS_PRESENT_UNMODELED"
    }:
        return CandidatePreflight("RISK_METADATA_UNVERIFIED", **shared)
    try:
        risk = await private.raw_account_risk_limits(symbol)
        if set(risk) != {symbol}:
            return CandidatePreflight("PRIVATE_RISK_SCOPE_UNVERIFIED", **shared)
        shorts = [r for r in risk[symbol]
                  if type(r) is dict and r.get("positionType") == 2
                  and r.get("symbol") == symbol]
        if len(shorts) != 1:
            return CandidatePreflight("PRIVATE_SHORT_TIER_UNVERIFIED", **shared)
    except Exception:
        return CandidatePreflight("PRIVATE_RISK_GET_FAILED", **shared)
    return CandidatePreflight(
        "CANDIDATE_METADATA_OBSERVED_NONAUTHORIZING",
        private_short_tier_observed=True, private_risk_scope_verified=True,
        **shared,
    )


async def run(symbol: str, *, environ: Mapping[str, str] | None = None) -> CandidatePreflight:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return CandidatePreflight("PAPER_KILL_REQUIRED")
    if not env.get("MEXC_API_KEY") or not env.get("MEXC_API_SECRET"):
        return CandidatePreflight("CREDENTIALS_MISSING")
    if type(symbol) is not str or not SYMBOL_PATTERN.fullmatch(symbol):
        return CandidatePreflight("INVALID_CANDIDATE_SYMBOL")
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient("https://api.mexc.com",
                             api_key=env["MEXC_API_KEY"], api_secret=env["MEXC_API_SECRET"])
    try:
        return await asyncio.wait_for(
            inspect_candidate(ReadOnlyMexcProbe(client), client.client, symbol),
            timeout=25.0,
        )
    except Exception:
        return CandidatePreflight("PROBE_FAILED")
    finally:
        await client.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="GET-only nonauthorizing candidate preflight")
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.symbol))
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    print("READ ONLY. NO ORDERS. NOT AUTHORIZED FOR LIVE TRADING.")
    if result.status != "CANDIDATE_METADATA_OBSERVED_NONAUTHORIZING":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
