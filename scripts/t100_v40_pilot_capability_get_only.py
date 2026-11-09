"""T100 WEB pilot capability witness: MEXC GET ONLY, no order transport.

After successful two-bookend flat account preflight, inspect a single
selected FUTURES contract's *public* availability and current private
short CROSS 1x risk tier. These GETs cannot establish API-key Futures
write permissions or certify any native stop amendment or trigger.

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
    if not (
        type(short.get("openType")) is int
        and short["openType"]==2
        and type(short.get("leverage")) is int
        and short["leverage"]==1
        and type(short.get("level")) is int
        and short["level"]>=1
    ):
        return report("PILOT_PRIVATE_SHORT_CROSS_ONE_X_NOT_OBSERVED_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_contract_tick_observed=True)
    try:
        max_private=_positive(short.get("maxVol"))
    except PilotCapabilityHalted:
        return report("PILOT_PRIVATE_MAX_VOLUME_INVALID_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_contract_tick_observed=True)
    if max_private<D(1):
        return report("PILOT_PRIVATE_SHORT_TIER_BELOW_ONE_CONTRACT_HALT",
                      flat_hedge_account_bookended=True,
                      public_futures_contract_eligible_observed=True,
                      one_contract_on_public_lot_grid=True,
                      current_private_short_cross_1x_tier_observed=True,
                      current_contract_tick_observed=True)
    return report(
        "PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
        flat_hedge_account_bookended=True,
        public_futures_contract_eligible_observed=True,
        current_private_short_cross_1x_tier_observed=True,
        one_contract_on_public_lot_grid=True,
        current_short_tier_supports_one_contract=True,
        current_contract_tick_observed=True,
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
    if flat.status!="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING":
        return PilotCapabilityReport(
            status="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT",symbol=symbol,
        )
    signed=KeychainSignedGetOnly(key=key,secret=secret)
    try:
        probe=ReadOnlyMexcProbe(signed)
        tiers=await probe.raw_account_risk_limits(symbol)
    except Exception:
        return PilotCapabilityReport(
            status="PILOT_PRIVATE_RISK_GET_UNVERIFIED_HALT",symbol=symbol,
            flat_hedge_account_bookended=True,
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
        flat_preflight_ok=True,
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
