"""First V40 canary: exact-position, STOP-MISSING emergency short close.

Close is a separate operation, NEVER an entry permit and NEVER a second
chance to submit an ambiguous order. Only one checkpoint-owned CROSS 1x
short with two signed account/symbol GET bookends, a confirmed ACKed
original entry, and NO live native stop/order qualifies.

The durable SQL claimant (separate module) commits an immutable
POST-MAY-HAVE-OCCURRED record BEFORE this transport gets called. A
timeout/HTTP rejection/restart is RECONCILE ONLY, no automatic retry.

MEXC Hedge Mode uses side=2 close short, type=5 market, openType=2 CROSS,
positionMode=1 and exact positionId. reduceOnly is one-way-only and MUST
NOT appear in the payload. This is not a short-entry pathway.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
import hashlib
import re
from typing import Any, Protocol

from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot
from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)

_SYMBOL=re.compile(r"[A-Z0-9]+_USDT\Z")
_ID=re.compile(r"[1-9][0-9]*\Z")
_OID=re.compile(r"t100v1e[a-f0-9]{20}\Z")


class V40EmergencyCloseHalted(RuntimeError):
    pass


def _num(raw:Any)->D:
    if type(raw) is bool or type(raw) not in (str,int,float,D):
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_NUMERIC_UNVERIFIED")
    try:
        value=D(str(raw))
    except (InvalidOperation,ValueError,TypeError):
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_NUMERIC_UNVERIFIED") from None
    if not value.is_finite():
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_NUMERIC_UNVERIFIED")
    return value


def _id(raw:Any)->str:
    if type(raw) not in (int,str) or _ID.fullmatch(str(raw)) is None:
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_IDENTITY_INVALID")
    return str(raw)


@dataclass(frozen=True)
class V40ExitFrame:
    positions:list[dict[str,Any]]
    symbol_positions:list[dict[str,Any]]
    stops:list[dict[str,Any]]
    symbol_stops:list[dict[str,Any]]
    counts:dict[str,int]
    hedge_mode:int


@dataclass(frozen=True)
class V40EmergencyCloseTarget:
    symbol:str
    position_id:str
    entry_external_oid:str
    original_order_id:str
    stop_order_id:str
    checkpoint_version:int
    quantity_contracts:D
    original_notional_usdt:D
    close_external_oid:str
    reference_ask:D
    cause:str="NATIVE_STOP_MISSING_DOUBLE_GET"
    entry_allowed:bool=False
    other_position_adopted:bool=False


def _stable_frame(
    frame:V40ExitFrame,checkpoint:PositionCheckpoint,
)->tuple[Any,...]:
    if (
        type(frame) is not V40ExitFrame
        or type(frame.positions) is not list
        or type(frame.symbol_positions) is not list
        or type(frame.stops) is not list
        or type(frame.symbol_stops) is not list
        or type(frame.counts) is not dict
        or type(frame.hedge_mode) is not int
        or frame.hedge_mode!=1
        or len(frame.positions)!=len(frame.symbol_positions)
        or len(frame.positions)!=1
        or frame.stops or frame.symbol_stops
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_ACCOUNT_NOT_SINGLE_UNPROTECTED_SHORT"
        )
    fields=(
        "sumCount","limitOrderCount","stopOrderCount",
        "planOrderCount","trackOrderCount",
    )
    if (
        set(frame.counts)!=set(fields)
        or any(type(frame.counts[k]) is not int or frame.counts[k]!=0
               for k in fields)
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_OTHER_OPEN_ORDERS_OR_STOPS"
        )
    pos=frame.positions[0]
    scoped=frame.symbol_positions[0]
    if type(pos) is not dict or type(scoped) is not dict:
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_POSITION_SHAPE_INVALID")
    critical=(
        "positionId","symbol","positionType","openType",
        "leverage","state","holdVol","frozenVol",
    )
    def classify(row:dict[str,Any])->tuple[Any,...]:
        if (
            row.get("symbol")!=checkpoint.symbol
            or _id(row.get("positionId"))!=checkpoint.position_id
            or row.get("positionType")!=2
            or row.get("openType")!=2
            or row.get("leverage")!=1
            or row.get("state")!=1
            or _num(row.get("holdVol"))!=checkpoint.contracts
            or _num(row.get("frozenVol"))!=D(0)
        ):
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_EXACT_MANAGED_SHORT_DRIFT"
            )
        return (
            str(row["positionId"]),row["symbol"],2,2,1,1,
            str(_num(row["holdVol"])),str(_num(row["frozenVol"])),
        )
    a,b=classify(pos),classify(scoped)
    if a!=b:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_SYMBOL_ACCOUNT_GET_DRIFT"
        )
    return a


def attest_missing_native_stop_close(
    *,
    ledger:LedgerSnapshot,first:V40ExitFrame,second:V40ExitFrame,
    origin_order:dict[str,Any],reference_ask:Any,
    original_notional_usdt:Any,
)->V40EmergencyCloseTarget:
    """Pure fail-closed target; never authorizes a POST by itself."""
    if (
        type(ledger) is not LedgerSnapshot
        or ledger.readonly_repeatable_read is not True
        or type(ledger.checkpoints) is not list or len(ledger.checkpoints)!=1
        or type(ledger.revisions) is not list or not ledger.revisions
        or type(ledger.intents) is not list or len(ledger.intents)!=1
        or type(ledger.stop_amendments) is not list
        or ledger.stop_amendments
        or type(origin_order) is not dict
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_DURABLE_SINGLE_OWNER_UNVERIFIED"
        )
    try:
        cp=checkpoint_from_row(ledger.checkpoints[0])
    except Exception:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_CHECKPOINT_UNVERIFIED"
        ) from None
    if (
        cp.lifecycle_state!="OPEN"
        or cp.checkpoint_version<1
        or _SYMBOL.fullmatch(cp.symbol) is None
        or cp.symbol in ("BTC_USDT","ETH_USDT")
        or _OID.fullmatch(cp.external_oid) is None
        or _ID.fullmatch(cp.position_id) is None
        or _ID.fullmatch(cp.exchange_order_id) is None
        or _ID.fullmatch(cp.stop_order_id) is None
        or cp.contracts<=0
        or cp.stop_price<cp.entry_price*D("1.75")
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_NOT_V40_PROTECTED_CHECKPOINT"
        )
    # Last immutable revision must correspond to the exactly pinned current
    # checkpoint; missing earlier history must not be silently adopted.
    revs=ledger.revisions
    if (
        len(revs)!=cp.checkpoint_version
        or any(type(r) is not dict or r.get("position_id")!=cp.position_id
               or r.get("checkpoint_version")!=i+1
               for i,r in enumerate(revs))
        or revs[-1].get("external_oid")!=cp.external_oid
        or revs[-1].get("exchange_order_id")!=cp.exchange_order_id
        or revs[-1].get("lifecycle_state")!="OPEN"
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_REVISION_CHAIN_MISSING"
        )
    intent=ledger.intents[0]
    if (
        type(intent) is not dict
        or intent.get("symbol")!=cp.symbol
        or intent.get("external_oid")!=cp.external_oid
        or str(intent.get("exchange_order_id"))!=cp.exchange_order_id
        or intent.get("state")!="ACK"
        or _num(intent.get("volume_contracts"))!=cp.contracts
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_ENTRY_INTENT_UNRESOLVED"
        )
    notional=_num(original_notional_usdt)
    ask=_num(reference_ask)
    if not D(0)<notional<=D("12.50") or ask<=0:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_PILOT_CAP_OR_MARKET_PRICE_INVALID"
        )
    if (
        origin_order.get("symbol")!=cp.symbol
        or str(origin_order.get("externalOid"))!=cp.external_oid
        or _id(origin_order.get("orderId"))!=cp.exchange_order_id
        or _id(origin_order.get("positionId"))!=cp.position_id
        or origin_order.get("side")!=3
        or origin_order.get("state")!=3
        or origin_order.get("openType")!=2
        or origin_order.get("leverage")!=1
        or _num(origin_order.get("dealVol"))!=cp.contracts
    ):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_ORIGIN_FILL_NOT_PROVEN"
        )
    if _stable_frame(first,cp)!=_stable_frame(second,cp):
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_SIGNED_BOOKENDS_DRIFT"
        )
    oid="t100v40x"+hashlib.sha256(
        f"{cp.external_oid}:{cp.position_id}:STOP_MISSING".encode("ascii")
    ).hexdigest()[:24]
    return V40EmergencyCloseTarget(
        symbol=cp.symbol,position_id=cp.position_id,
        entry_external_oid=cp.external_oid,
        original_order_id=cp.exchange_order_id,
        stop_order_id=cp.stop_order_id,
        checkpoint_version=cp.checkpoint_version,
        quantity_contracts=cp.contracts,
        original_notional_usdt=notional,
        close_external_oid=oid,reference_ask=ask,
    )


@dataclass(frozen=True)
class V40EmergencyCloseAck:
    status:str
    external_oid:str
    order_id:str|None=None
    post_may_have_occurred:bool=True
    no_second_post_allowed:bool=True
    flat_account_verified:bool=False
    entry_allowed:bool=False


class V40EmergencyCloseClaim(Protocol):
    async def reserve_one(self,target:V40EmergencyCloseTarget)->bool: ...


class V40EmergencyCloseAdapter:
    """One documented close POST after a committed durable claim.

    The caller MUST call only after reserve_one() returns True. A returned
    success ACK is not evidence of execution: independent order/deal and
    flat-account GETs plus durable CLOSED checkpoint are mandatory.
    """
    def __init__(self,client:Any)->None:
        self.client=client

    async def send_once(
        self,target:V40EmergencyCloseTarget,
        *,committed_claim:bool,
    )->V40EmergencyCloseAck:
        if (
            type(target) is not V40EmergencyCloseTarget
            or committed_claim is not True
            or _ID.fullmatch(target.position_id) is None
            or target.quantity_contracts<=0
            or target.original_notional_usdt>D("12.50")
            or target.reference_ask<=0
            or not target.reference_ask.is_finite()
            or _SYMBOL.fullmatch(target.symbol) is None
            or _OID.fullmatch(target.entry_external_oid) is None
            or not target.close_external_oid.startswith("t100v40x")
        ):
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_POST_CLAIM_OR_IDENTITY_INVALID"
            )
        payload={
            "symbol":target.symbol,
            "price":str(target.reference_ask),
            "vol":str(target.quantity_contracts),
            "leverage":1,
            "side":2,             # CLOSE SHORT, never OPEN LONG
            "type":5,             # MARKET
            "openType":2,         # CROSS
            "positionMode":1,     # HEDGE (reduceOnly forbidden)
            "positionId":int(target.position_id),
            "externalOid":target.close_external_oid,
        }
        try:
            result=await self.client._private_post(
                "/api/v1/private/order/create",payload,
            )
            raw=result.get("orderId") if type(result) is dict else result
            order=_id(raw)
            return V40EmergencyCloseAck(
                "V40_EMERGENCY_CLOSE_POST_ACK_RECONCILE_ONLY",
                external_oid=target.close_external_oid,order_id=order,
            )
        except BaseException:
            # The venue may have executed despite timeout/connection loss.
            return V40EmergencyCloseAck(
                "V40_EMERGENCY_CLOSE_POST_OUTCOME_UNKNOWN_NO_RETRY",
                external_oid=target.close_external_oid,
            )


async def close_once_after_durable_claim(
    *,target:V40EmergencyCloseTarget,
    claimant:V40EmergencyCloseClaim,
    adapter:V40EmergencyCloseAdapter,
)->V40EmergencyCloseAck:
    """SQL commit MUST finish before there is any possible venue POST."""
    if type(target) is not V40EmergencyCloseTarget:
        raise V40EmergencyCloseHalted("V40_EMERGENCY_CLOSE_TARGET_REQUIRED")
    try:
        claimed=await claimant.reserve_one(target)
    except BaseException:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_CLAIM_DB_UNCERTAIN_NO_POST"
        ) from None
    if claimed is not True:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_ALREADY_CLAIMED_NO_SECOND_POST"
        )
    return await adapter.send_once(target,committed_claim=True)
