"""T100 WEB pilot capability witness: MEXC GET ONLY, no order transport.

After successful two-bookend flat account preflight, inspect a single
selected FUTURES contract's *public* availability and current private
short risk tier and separate position/leverage configuration. These GETs
cannot establish API-key Futures write permissions or certify any native stop amendment or trigger.

Private GET is restricted by KeychainSignedGetOnly, public GET is
restricted to a single contract-country endpoint. No POST, no DB, no
Render, no service deployment, no account settings changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict,dataclass
from decimal import Decimal as D,InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from scripts.t100_v40_pilot_get_only import (
    BASE,KEY_ACCOUNT,SECRET_ACCOUNT,
    KeychainSignedGetOnly,_from_keychain,observe_live_pilot,
)

COUNTRY_PATH="/api/v1/contract/detail/country"


class PilotCapabilityHalted(RuntimeError):
    """Only fixed diagnostic IDs; never transport/Keychain information."""


@dataclass(frozen=True)
class PilotCapabilityReport:
    status: str
    symbol: str
    flat_hedge_account_bookended: bool=False
    public_futures_contract_eligible_observed: bool=False
    current_private_short_cross_1x_tier_observed: bool=False
    current_private_short_risk_limit_observed: bool=False
    private_short_position_leverage_observed: bool=False
    private_capability_drift_source: str="NONE"
    short_margin_mode_observed: str="UNKNOWN"
    short_leverage_observed: int|None=None
    private_risk_limit_mode_fields_missing: bool=False
    one_contract_on_public_lot_grid: bool=False
    current_short_tier_supports_one_contract: bool=False
    current_contract_tick_observed: bool=False
    api_key_futures_write_permission_verified: bool=False
    api_key_subaccount_identity_independently_verified: bool=False
    native_market_stop_amendment_verified: bool=False
    native_stop_terminal_execution_verified: bool=False
    future_stressed_risk_tiers_verified: bool=False
    cross_collateral_verified: bool=False
    account_snapshot_atomicity_verified: bool=False
    db_mutations: int=0
    mexc_posts: int=0
    live_authorization: bool=False
    entry_allowed: bool=False


def _positive(value: Any) -> D:
    if value is None or type(value) is bool:
        raise PilotCapabilityHalted("PILOT_CONTRACT_QUANTITY_SCHEMA_INVALID")
    try:
        n=D(str(value))
    except (ValueError,TypeError,InvalidOperation):
        raise PilotCapabilityHalted("PILOT_CONTRACT_QUANTITY_SCHEMA_INVALID") from None
    if not n.is_finite() or n<=0:
        raise PilotCapabilityHalted("PILOT_CONTRACT_QUANTITY_SCHEMA_INVALID")
    return n


def assess_pilot_capability(
    *,
    symbol: str,
    contract: Any,
    private_risk: Any,
    private_leverage: Any,
    flat_preflight_ok: bool,
) -> PilotCapabilityReport:
    if (
        type(symbol) is not str
        or not re.fullmatch(r"[A-Z0-9]+_USDT",symbol)
        or type(flat_preflight_ok) is not bool
    ):
        raise PilotCapabilityHalted("PILOT_CAPABILITY_IDENTITY_INVALID")
    def report(status: str,**kw:Any)->PilotCapabilityReport:
        return PilotCapabilityReport(status=status,symbol=symbol,**kw)

    if not flat_preflight_ok:
        return report("PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT")
    if type(contract) is not dict or any(
        type(contract.get(k)) is not int for k in
        ("state","futureType","positionOpenType")
    ):
        return report("PILOT_PUBLIC_CONTRACT_SHAPE_UNVERIFIED_HALT",
                      flat_hedge_account_bookended=True)
    eligible=all((
        contract.get("symbol")==symbol,
        contract.get("apiAllowed") is True,
        contract["state"]==0,
        contract["futureType"]==1,
        contract["positionOpenType"] in (2,3),
        contract.get("quoteCoin")=="USDT",
        contract.get("settleCoin")=="USDT",
    ))
    if not eligible:
        return report("PILOT_PUBLIC_CONTRACT_NOT_ELIGIBLE_HALT",
                      flat_hedge_account_bookended=True)
    try:
        min_vol=_positive(contract.get("minVol"))
        max_vol=_positive(contract.get("maxVol"))
        lot=_positive(contract.get("volUnit"))
        tick=_positive(contract.get("priceUnit"))
        size=_positive(contract.get("contractSize"))
    except PilotCapabilityHalted:
        return report("PILOT_PUBLIC_CONTRACT_GRID_INVALID_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True)

    lot_valid=all((
        min_vol<=max_vol,
        min_vol%lot==0,
        max_vol%lot==0,
        min_vol<=D(1)<=max_vol,
        D(1)%lot==0,
    ))
    if not lot_valid:
        return report("PILOT_ONE_CONTRACT_NOT_ON_LOT_GRID_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      current_contract_tick_observed=True)
    if (
        type(private_risk) is not dict
        or set(private_risk)!={symbol}
        or type(private_risk[symbol]) is not list
    ):
        return report("PILOT_PRIVATE_RISK_SCOPE_UNVERIFIED_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_contract_tick_observed=True)
    rows=private_risk[symbol]
    if any(type(r) is not dict or r.get("symbol")!=symbol for r in rows):
        return report("PILOT_PRIVATE_RISK_ROWS_MALFORMED_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_contract_tick_observed=True)
    shorts=[
        r for r in rows
        if type(r.get("positionType")) is int and r["positionType"]==2
    ]
    if len(shorts)!=1:
        return report("PILOT_PRIVATE_SHORT_TIER_NOT_UNIQUE_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_contract_tick_observed=True)
    short=shorts[0]
    base=dict(
        flat_hedge_account_bookended=True,
        public_futures_contract_eligible_observed=True,
        one_contract_on_public_lot_grid=True,
        current_contract_tick_observed=True,
    )
    if type(short.get("level")) is not int or short["level"]<1:
        return report("PILOT_PRIVATE_SHORT_RISK_TIER_INVALID_HALT",**base)
    try:
        max_private=_positive(short.get("maxVol"))
    except PilotCapabilityHalted:
        return report("PILOT_PRIVATE_MAX_VOLUME_INVALID_HALT",**base)
    base["current_private_short_risk_limit_observed"]=True

    # account/risk_limit's primary contract describes liquidation tiers,
    # NOT authoritative leverage configuration. Historic versions may
    # omit openType/leverage altogether. Use the separately documented
    # private/position/leverage GET for the actual SHORT mode and 1x.
    if (
        type(private_leverage) is not list
        or not 1<=len(private_leverage)<=2
        or any(type(row) is not dict for row in private_leverage)
    ):
        return report("PILOT_PRIVATE_LEVERAGE_ROWS_UNVERIFIED_HALT",**base)
    seen=set()
    for row in private_leverage:
        side=row.get("positionType")
        if (
            type(side) is not int or side not in (1,2)
            or side in seen
            or ("symbol" in row and row["symbol"]!=symbol)
            or type(row.get("openType")) is not int
            or row["openType"] not in (1,2)
            or type(row.get("leverage")) is not int
            or not 1<=row["leverage"]<=1000
        ):
            return report("PILOT_PRIVATE_LEVERAGE_ROWS_UNVERIFIED_HALT",**base)
        seen.add(side)
    leverages=[r for r in private_leverage if r["positionType"]==2]
    if len(leverages)!=1:
        return report("PILOT_PRIVATE_SHORT_LEVERAGE_MISSING_HALT",**base)
    config=leverages[0]
    base.update(
        private_short_position_leverage_observed=True,
        short_margin_mode_observed=(
            "CROSS" if config["openType"]==2 else "ISOLATED"
        ),
        short_leverage_observed=config["leverage"],
        private_risk_limit_mode_fields_missing=(
            "openType" not in short or "leverage" not in short
        ),
    )
    if config["openType"]!=2 or config["leverage"]!=1:
        return report("PILOT_PRIVATE_SHORT_CONFIG_NOT_CROSS_ONE_X_HALT",**base)

    # When risk-limit GET also supplies mode/leverage, disagreements
    # between two signed private endpoints are NOT silently waived.
    # Do not infer CROSS from merely seeing a risk tier.
    if (
        ("openType" in short and (
            type(short["openType"]) is not int
            or short["openType"]!=config["openType"]
        ))
        or ("leverage" in short and (
            type(short["leverage"]) is not int
            or short["leverage"]!=config["leverage"]
        ))
    ):
        return report("PILOT_PRIVATE_RISK_VS_LEVERAGE_CONFLICT_HALT",**base)

    base["current_private_short_cross_1x_tier_observed"]=True
    if max_private<D(1):
        return report("PILOT_PRIVATE_SHORT_TIER_BELOW_ONE_CONTRACT_HALT",**base)
    return report(
        "PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
        current_short_tier_supports_one_contract=True,
        **base,
    )


async def observe_pilot_capability(
    *, key: str,secret: str,symbol: str="BTC_USDT",
) -> PilotCapabilityReport:
    if (
        type(symbol) is not str
        or not re.fullmatch(r"[A-Z0-9]+_USDT",symbol)
    ):
        raise PilotCapabilityHalted("PILOT_SYMBOL_INVALID")
    flat=await observe_live_pilot(key=key,secret=secret)
    if not (
        flat.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
        and flat.samples==2
        and flat.position_count==0
        and flat.native_stop_count==0
        and flat.hedge_mode_observed is True
        and flat.inventory_bookends_equal is True
        and flat.live_authorization is False
        and flat.mexc_posts==0
        and flat.db_mutations==0
    ):
        return PilotCapabilityReport(
            status="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT",symbol=symbol,
        )
    signed=KeychainSignedGetOnly(key=key,secret=secret)
    try:
        probe=ReadOnlyMexcProbe(signed)
        try:
            tiers=await probe.raw_account_risk_limits(symbol)
        except Exception:
            return PilotCapabilityReport(
                status="PILOT_PRIVATE_RISK_GET_UNVERIFIED_HALT",symbol=symbol,
                flat_hedge_account_bookended=True,
            )
        try:
            leverages=await probe.raw_position_leverage(symbol)
        except Exception:
            return PilotCapabilityReport(
                status="PILOT_PRIVATE_LEVERAGE_GET_UNVERIFIED_HALT",
                symbol=symbol,flat_hedge_account_bookended=True,
            )
        try:
            tiers_second=await probe.raw_account_risk_limits(symbol)
            leverages_second=await probe.raw_position_leverage(symbol)
            # Diagnose WHICH signed endpoint drifted without exposing raw
            # responses, weakening exact identity checks, or allowing trades.
            risk_stable=(
                json.dumps(tiers,sort_keys=True,
                           allow_nan=False,separators=(",",":"))
                == json.dumps(tiers_second,sort_keys=True,
                              allow_nan=False,separators=(",",":"))
            )
            leverage_stable=(
                json.dumps(leverages,sort_keys=True,
                           allow_nan=False,separators=(",",":"))
                == json.dumps(leverages_second,sort_keys=True,
                              allow_nan=False,separators=(",",":"))
            )
        except Exception:
            return PilotCapabilityReport(
                status="PILOT_PRIVATE_CAPABILITY_SECOND_GET_UNVERIFIED_HALT",
                symbol=symbol,flat_hedge_account_bookended=True,
            )
        if not risk_stable or not leverage_stable:
            drift_source=(
                "BOTH" if not risk_stable and not leverage_stable
                else "RISK_LIMIT" if not risk_stable
                else "POSITION_LEVERAGE"
            )
            return PilotCapabilityReport(
                status="PILOT_PRIVATE_CAPABILITY_GET_DRIFT_HALT",
                symbol=symbol,flat_hedge_account_bookended=True,
                private_capability_drift_source=drift_source,
            )
    finally:
        await signed.close()

    try:
        async with httpx.AsyncClient(
            base_url=BASE,trust_env=False,timeout=10,
            follow_redirects=False,
        ) as client:
            response=await client.get(
                COUNTRY_PATH,params={"symbol":symbol},
            )
            response.raise_for_status()
            body=response.json()
            if not (
                type(body) is dict and body.get("success") is True
                and type(body.get("code")) is int
                and body["code"]==0 and type(body.get("data")) is dict
            ):
                raise PilotCapabilityHalted(
                    "PILOT_PUBLIC_CONTRACT_RESPONSE_UNVERIFIED"
                )
            contract=body["data"]
    except Exception:
        return PilotCapabilityReport(
            status="PILOT_PUBLIC_CONTRACT_GET_UNVERIFIED_HALT",
            symbol=symbol,flat_hedge_account_bookended=True,
        )
    return assess_pilot_capability(
        symbol=symbol,contract=contract,private_risk=tiers,
        private_leverage=leverages,flat_preflight_ok=True,
    )


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keychain-service",required=True,
        help="Explicit T100 web-pilot Keychain service; never use BOXWAVE",
    )
    parser.add_argument("--symbol",default="BTC_USDT")
    parser.add_argument(
        "--output",default="~/Downloads/t100_v40_pilot_capability_get_only.json",
    )
    args=parser.parse_args()
    try:
        key=_from_keychain(args.keychain_service,KEY_ACCOUNT)
        secret=_from_keychain(args.keychain_service,SECRET_ACCOUNT)
        outcome=asyncio.run(observe_pilot_capability(
            key=key,secret=secret,symbol=args.symbol,
        ))
    except Exception:
        outcome=PilotCapabilityReport(
            status="PILOT_GET_ONLY_CAPABILITY_DIAGNOSTIC_HALTED",
            symbol=args.symbol if re.fullmatch(r"[A-Z0-9]+_USDT",args.symbol) else "INVALID",
        )
    path=Path(args.output).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(asdict(outcome),indent=2,sort_keys=True)+"\n")
    path.chmod(0o600)
    print("MEXC_GET_ONLY=true POST_COUNT=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(outcome),sort_keys=True))


if __name__=="__main__":
    main()
