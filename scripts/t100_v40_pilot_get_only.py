"""One-shot T100 V40 pilot-account *GET-ONLY* Keychain preflight.

No order, stop, margin or account-configuration mutation methods exist in
this script's transport. Account selection is explicit: --keychain-service
is mandatory, because older pilot and newer web-login pilot accounts may
use different Keychain entries. Merely naming a service does NOT prove
which MEXC account it belongs to.

Uses two full signed private GET bookends: positions, active TP/SL,
account order counts, USDT asset economics, and hedge/one-way mode.
Reports only a redacted, non-authorizing account observation. A flat
snapshot is NOT independent proof of exchange atomicity, private POST
permission, correct historical stop execution, or subaccount identity.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import re
import subprocess
import time
from dataclasses import asdict,dataclass
from decimal import Decimal as D,InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from app.t100_live_readonly_probe import ReadOnlyMexcProbe

BASE="https://api.mexc.com"
KEY_ACCOUNT="MEXC_API_KEY"
SECRET_ACCOUNT="MEXC_API_SECRET"


class PilotPreflightHalted(RuntimeError):
    """A short stable code, never a raw secret, API payload or URL."""


class KeychainSignedGetOnly:
    """No POST method and no exposed low-level HTTP client."""

    def __init__(self, *, key: str, secret: str):
        if not key or not secret:
            raise PilotPreflightHalted("KEYCHAIN_CREDENTIALS_UNAVAILABLE")
        self._key=key
        self._secret=secret
        self._client=httpx.AsyncClient(
            base_url=BASE,trust_env=False,
            follow_redirects=False,timeout=10,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _private_get(
        self, path: str, params: dict[str,Any]|None=None,
    ) -> Any:
        if (
            path not in (
                "/api/v1/private/position/open_positions",
                "/api/v1/private/stoporder/open_orders",
                "/api/v1/private/order/open_order_total_count",
                "/api/v1/private/account/assets",
                "/api/v1/private/position/position_mode",
            )
            or params is not None
        ):
            raise PilotPreflightHalted("GET_ONLY_PATH_NOT_ALLOWLISTED")
        stamp=str(int(time.time()*1000))
        message=(self._key+stamp).encode()
        signature=hmac.new(
            self._secret.encode(),message,hashlib.sha256,
        ).hexdigest()
        try:
            response=await self._client.get(
                path,headers={
                    "ApiKey":self._key,
                    "Request-Time":stamp,
                    "Signature":signature,
                    "Recv-Window":"10",
                },
            )
            response.raise_for_status()
            payload=response.json()
        except Exception:
            raise PilotPreflightHalted("SIGNED_GET_HTTP_FAILED") from None
        if (
            type(payload) is not dict
            or payload.get("success") is not True
            or type(payload.get("code")) is not int
            or payload["code"]!=0
        ):
            raise PilotPreflightHalted("SIGNED_GET_RESPONSE_FAILED")
        return payload.get("data")


@dataclass(frozen=True)
class PilotInventory:
    positions: list[dict[str,Any]]
    stops: list[dict[str,Any]]
    counts: dict[str,int]
    assets: list[dict[str,Any]]
    position_mode: int


@dataclass(frozen=True)
class PilotGetOnlyResult:
    status: str
    samples: int=0
    position_count: int | None=None
    native_stop_count: int | None=None
    account_equity_usdt: str | None=None
    available_balance_usdt: str | None=None
    hedge_mode_observed: bool=False
    inventory_bookends_equal: bool=False
    account_scope_independently_verified: bool=False
    cross_margin_collateral_verified: bool=False
    private_post_permission_verified: bool=False
    exact_native_stop_lifecycle_verified: bool=False
    db_mutations: int=0
    mexc_posts: int=0
    live_authorization: bool=False


def _nonnegative(x: Any) -> D:
    try:
        if x is None or type(x) is bool:
            raise InvalidOperation
        n=D(str(x))
    except (InvalidOperation,ValueError,TypeError):
        raise PilotPreflightHalted("USDT_ASSET_FIELD_INVALID") from None
    if not n.is_finite() or n<0:
        raise PilotPreflightHalted("USDT_ASSET_FIELD_INVALID")
    return n


def evaluate_pilot_flat_bookends(
    first: PilotInventory,second: PilotInventory,
) -> PilotGetOnlyResult:
    if type(first) is not PilotInventory or type(second) is not PilotInventory:
        raise PilotPreflightHalted("PILOT_BOOKEND_SHAPE_INVALID")
    required=(
        "sumCount","stopOrderCount","limitOrderCount",
        "planOrderCount","trackOrderCount",
    )

    def normalized(s: PilotInventory) -> tuple[str,D,D]:
        if (
            type(s.position_mode) is not int
            or s.position_mode!=1
            or type(s.positions) is not list
            or type(s.stops) is not list
            or s.positions or s.stops
            or type(s.counts) is not dict
            or any(type(s.counts.get(k)) is not int or s.counts[k]!=0
                   for k in required)
            or type(s.assets) is not list
            or any(type(x) is not dict for x in s.assets)
        ):
            raise PilotPreflightHalted("PILOT_NOT_FLAT_HEDGE_WITH_ZERO_ORDERS")
        records=[r for r in s.assets if r.get("currency")=="USDT"]
        if len(records)!=1:
            raise PilotPreflightHalted("PILOT_USDT_ASSET_MISSING_OR_DUPLICATE")
        a=records[0]
        needed=("equity","availableBalance","positionMargin",
                "unrealized","cashBalance","frozenBalance",
                "bonus","debtAmount","contributeMarginAmount")
        if any(k not in a for k in needed):
            raise PilotPreflightHalted("PILOT_USDT_ASSET_FIELDS_MISSING")
        vals={k:_nonnegative(a[k]) for k in needed}
        if (
            vals["equity"]<=0
            or vals["availableBalance"]>vals["equity"]
            or vals["positionMargin"]!=0
            or vals["unrealized"]!=0
            or vals["debtAmount"]!=0
        ):
            raise PilotPreflightHalted("PILOT_USDT_ECONOMICS_INCONSISTENT")
        try:
            frozen=json.dumps({
                "positions":s.positions,"stops":s.stops,
                "counts":s.counts,
                "assets":s.assets,"position_mode":s.position_mode,
            },sort_keys=True,separators=(",",":"),allow_nan=False)
        except (ValueError,TypeError):
            raise PilotPreflightHalted("PILOT_BOOKEND_NONCANONICAL") from None
        return frozen,vals["equity"],vals["availableBalance"]

    key_a,e_a,a_a=normalized(first)
    key_b,e_b,a_b=normalized(second)
    if key_a!=key_b or e_a!=e_b or a_a!=a_b:
        raise PilotPreflightHalted("PILOT_SIGNED_GET_BOOKEND_CHANGED")
    return PilotGetOnlyResult(
        status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
        samples=2,position_count=0,native_stop_count=0,
        account_equity_usdt=str(e_b),
        available_balance_usdt=str(a_b),
        hedge_mode_observed=True,
        inventory_bookends_equal=True,
    )


def _from_keychain(service: str, account: str) -> str:
    if (
        not 1<=len(service)<=160
        or re.fullmatch(r"[A-Za-z0-9_.-]+",service) is None
    ):
        raise PilotPreflightHalted("KEYCHAIN_SERVICE_NAME_INVALID")
    try:
        p=subprocess.run([
            "/usr/bin/security","find-generic-password",
            "-s",service,"-a",account,"-w",
        ],capture_output=True,timeout=15,check=False)
    except Exception:
        raise PilotPreflightHalted("KEYCHAIN_READ_FAILED") from None
    if p.returncode!=0 or not p.stdout.strip():
        raise PilotPreflightHalted("KEYCHAIN_CREDENTIAL_NOT_FOUND")
    return p.stdout.decode("utf-8").strip()


async def observe_live_pilot(
    *, key: str,secret: str,
) -> PilotGetOnlyResult:
    transport=KeychainSignedGetOnly(key=key,secret=secret)
    probe=ReadOnlyMexcProbe(transport)
    async def sample() -> PilotInventory:
        return PilotInventory(
            positions=await probe.raw_open_positions(),
            stops=await probe.raw_open_stops(),
            counts=await probe.raw_open_order_counts(),
            assets=await probe.raw_account_assets(),
            position_mode=await probe.raw_position_mode(),
        )
    try:
        started=time.monotonic()
        first=await sample()
        second=await sample()
        if time.monotonic()-started>20:
            raise PilotPreflightHalted("PILOT_FULL_GET_CYCLE_STALE")
        return evaluate_pilot_flat_bookends(first,second)
    except PilotPreflightHalted:
        raise
    except Exception:
        raise PilotPreflightHalted("PILOT_SIGNED_GET_UNVERIFIED") from None
    finally:
        await transport.close()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keychain-service",required=True,
        help="Explicit Mac Keychain service for the intended PILOT account",
    )
    parser.add_argument(
        "--output",default="~/Downloads/t100_v40_pilot_get_only.json",
    )
    args=parser.parse_args()
    try:
        key=_from_keychain(args.keychain_service,KEY_ACCOUNT)
        secret=_from_keychain(args.keychain_service,SECRET_ACCOUNT)
        outcome=asyncio.run(observe_live_pilot(key=key,secret=secret))
    except PilotPreflightHalted as exc:
        outcome=PilotGetOnlyResult(status=str(exc))
    except Exception:
        outcome=PilotGetOnlyResult(status="PILOT_PREFLIGHT_UNKNOWN_FAILED")

    path=Path(args.output).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(asdict(outcome),sort_keys=True,indent=2)+"\n")
    path.chmod(0o600)
    print("MEXC_GET_ONLY=true POST_COUNT=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(outcome),sort_keys=True))


if __name__=="__main__":
    main()
