"""Manual PUBLIC-GET-only V40 candidate quantity / order-cap audit.

Price and target USDT notional are caller-supplied HYPOTHETICAL inputs.
This command does NOT use private account assets or market orderbook data.
It does not place/cancel/modify orders, connect to Render, query any private
account endpoint, or authorize live execution. Current public metadata is
double-read; any drift fails closed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_public_v40_capacity import (
    PublicV40CapacityReview, assess_public_v40_capacity,
)

_PATH = "/api/v1/contract/detail/country"
_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")


def shareable(report: PublicV40CapacityReview) -> dict[str, Any]:
    def numerical(value: Any) -> str | None:
        return str(value) if value is not None else None

    return {
        "status": report.status,
        "required_lots": numerical(report.required_lots_rounded_down),
        "public_max_single_order_lots":
            numerical(report.public_max_single_order_lots),
        "public_max_position_tier_lots":
            numerical(report.public_max_position_tier_lots),
        "published_max_position_notional_usdt":
            numerical(report.public_max_position_notional_usdt),
        "hypothetical_target_notional_usdt":
            numerical(report.desired_notional_usdt),
        "hypothetical_quantized_notional_usdt":
            numerical(report.modeled_lots_notional_usdt),
        "quantized_fraction_of_target":
            numerical(report.modeled_notional_fraction),
        "fits_public_single_order_limit":
            report.fits_public_single_order_limit,
        "fits_public_position_tier":
            report.fits_public_position_tier,
        "published_stop_gap_research_computed":
            report.modeled_stop_gap_notional_usdt is not None,
        "private_current_cap_verified": False,
        "private_current_cross_verified": False,
        "account_specific_order_access_verified": False,
        "depth_and_execution_capacity_verified": False,
        "liquidation_before_stop_certified": False,
        "future_tiers_certified": False,
        "account_collateral_certified": False,
        "native_stop_protection_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }


async def inspect(
    public: Any, symbol: str, *,
    target_notional_usdt: str, entry_reference_price: str, leverage: int,
) -> dict[str, Any]:
    def blocked(status: str) -> dict[str, Any]:
        return shareable(PublicV40CapacityReview(status=status))

    if (type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None
        or symbol in {"BTC_USDT", "ETH_USDT"}):
        return blocked("V40_CAPACITY_SYMBOL_INVALID")
    if public is None or not callable(getattr(public, "get", None)):
        return blocked("V40_CAPACITY_CLIENT_INVALID")
    rows: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            response = await public.get(_PATH, params={"symbol": symbol})
            response.raise_for_status()
            envelope = response.json()
        except Exception:
            return blocked("V40_CAPACITY_PUBLIC_GET_FAILED")
        if (
            type(envelope) is not dict
            or envelope.get("success") is not True
            or type(envelope.get("code")) is not int
            or envelope["code"] != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol
        ):
            return blocked("V40_CAPACITY_PUBLIC_RESPONSE_UNVERIFIED")
        rows.append(envelope["data"])
    try:
        fingerprint = tuple(
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for row in rows
        )
    except (TypeError, ValueError):
        return blocked("V40_CAPACITY_PUBLIC_RESPONSE_UNVERIFIED")
    if fingerprint[0] != fingerprint[1]:
        return blocked("V40_CAPACITY_PUBLIC_METADATA_DRIFT")
    report = assess_public_v40_capacity(
        rows[1],
        desired_notional_usdt=target_notional_usdt,
        entry_reference_price=entry_reference_price,
        leverage=leverage,
    )
    return shareable(report)


async def run(
    symbol: str, *, target_notional_usdt: str,
    entry_reference_price: str, leverage: int = 1,
    environ: Mapping[str, str] | None = None,
    public: Any = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return shareable(PublicV40CapacityReview("V40_CAPACITY_PAPER_KILL_REQUIRED"))
    if public is not None:
        return await inspect(
            public, symbol, target_notional_usdt=target_notional_usdt,
            entry_reference_price=entry_reference_price, leverage=leverage,
        )
    try:
        import httpx
    except ImportError:
        return shareable(PublicV40CapacityReview("V40_CAPACITY_HTTPX_MISSING"))
    try:
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com", follow_redirects=False,
            timeout=15.0,
            headers={"User-Agent": "T100-public-v40-capacity-research/1.0"},
        ) as client:
            return await asyncio.wait_for(
                inspect(
                    client, symbol, target_notional_usdt=target_notional_usdt,
                    entry_reference_price=entry_reference_price,
                    leverage=leverage,
                ),
                timeout=35.0,
            )
    except asyncio.TimeoutError:
        return shareable(PublicV40CapacityReview("V40_CAPACITY_TIMED_OUT"))
    except Exception:
        return shareable(PublicV40CapacityReview("V40_CAPACITY_READ_FAILED"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public-only current contract-lot capacity, no orders",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--target-notional-usdt", required=True)
    parser.add_argument("--entry-reference-price", required=True)
    parser.add_argument("--leverage", type=int, default=1)
    options = parser.parse_args()
    try:
        report = asyncio.run(run(
            options.symbol, target_notional_usdt=options.target_notional_usdt,
            entry_reference_price=options.entry_reference_price,
            leverage=options.leverage,
        ))
    except Exception:
        report = shareable(PublicV40CapacityReview("V40_CAPACITY_READ_FAILED"))
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PUBLIC GET ONLY. HYPOTHETICAL INPUTS. NO LIVE ORDER AUTHORIZATION.")


if __name__ == "__main__":
    main()
