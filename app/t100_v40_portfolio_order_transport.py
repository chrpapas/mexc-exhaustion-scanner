"""V40 one-per-cycle genuine Stage2 FOUR-SLOT MEXC CROSS order transport.

Do not conflate four independent SLOT positions with exchange batch orders.
Each SHORT uses exactly one durable externalOid, one irrevocable POST,
and a native attached SL75. No retry on a lost HTTP acknowledgement.

This adapter is *not* instantiated by a Render worker. The standalone
portfolio permit stays NOT_CERTIFIED and the independent guardian is
currently latched; therefore no real POST is authorized by shipping it.

Guarding sequence immediately before any exchange mutation:
  certified portfolio-only typed signal permit -> exact immutable lot/stop
  -> independent full-account/stop/ledger signed snapshot (0-3 owned)
  -> fresh scope-004 FLAT or protected NONFLAT guardian/outbox
  -> current authenticated USDT collateral and country contract limits
  -> actual private CROSS-short leverage and risk tier
  -> second independent full-account/stop/ledger snapshot, guardian and
     collateral bookend; then ONE exact MEXC POST.
All errors are finite codes, and no raw GET/SQL bodies or credentials leak.

Caution: unchanged GET bookends are not exchange atomicity, and nominal
initial-margin feasibility is not liquidation-stress certification.
The independent portfolio release evidence must certify those separately.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D,InvalidOperation
import re
from typing import Any

from app.t100_live_authorization import (
    LiveMutationPermit,V40_PORTFOLIO_SCOPE,
    require_certified_transport_permit,
)
from app.t100_live_execution import EntryIntent,deterministic_t100_entry_oid
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    inspect_existing_four_slots_get_only,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,V40GuardianPostGateEvidence,
)
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,V40NonflatGuardianEvidence,
)
from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeveragePlan,STATUS_FEASIBLE,
)

POST_PATH="/api/v1/private/order/create"
FLAT_OK="V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING"
NONFLAT_OK="V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING"


class V40PortfolioPostHalted(RuntimeError):
    """Fixed-category redacted NO-POST or UNKNOWN-POST error."""


@dataclass(frozen=True)
class V40PortfolioPostContext:
    expected_occupied_slots:int
    expected_owned_position_ids:tuple[str,...]
    frozen_order_notional_usdt:D
    frozen_portfolio_gross_after_entry_usdt:D
    frozen_equity_at_plan_usdt:D
    protected_remaining_margin_headroom_usdt:D
    fee_reserve_usdt:D
    leverage_plan:V40CrossLeveragePlan
    # Only the already released, typed full-portfolio permit can issue a
    # potential mutation. This context is NEVER a permit.


@dataclass(frozen=True)
class V40PortfolioPostReview:
    status:str
    occupied_slots:int=0
    selected_exchange_cross_leverage:int|None=None
    signed_equity_usdt:D|None=None
    signed_available_margin_usdt:D|None=None
    account_stop_and_ledger_bookends_verified:bool=False
    guardian_double_verified:bool=False
    private_margin_tier_verified:bool=False
    contract_exact_lot_and_stop_verified:bool=False
    live_authorization:bool=False
    entry_allowed:bool=False
    mexc_posts:int=0
    database_writes:int=0


def _positive(x:object,*,allow_zero:bool=False)->D:
    if type(x) is bool or x is None:
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_NONFINITE_VENUE_AMOUNT_NO_POST")
    try:
        n=D(str(x))
    except (ValueError,TypeError,InvalidOperation):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_NONFINITE_VENUE_AMOUNT_NO_POST") from None
    if not n.is_finite() or (n<0 if allow_zero else n<=0):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_NONFINITE_VENUE_AMOUNT_NO_POST")
    return n


def _asset_usdt(rows:object)->tuple[D,D]:
    if type(rows) is not list or not rows or any(type(x) is not dict for x in rows):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_ASSET_ROWS_UNVERIFIED_NO_POST")
    usdt=[x for x in rows if x.get("currency")=="USDT"]
    if len(usdt)!=1 or any(
        r is not usdt[0] and
        (r.get("currency") is None or _positive(r.get("equity"),allow_zero=True)!=0)
        for r in rows
    ):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_NONUSDT_EQUITY_NO_POST")
    u=usdt[0]
    equity=_positive(u.get("equity"))
    available=_positive(u.get("availableOpen"),allow_zero=True)
    reported_available=_positive(u.get("availableBalance"),allow_zero=True)
    if (
        available>reported_available
        or reported_available>equity+_positive(u.get("positionMargin"),allow_zero=True)
        or _positive(u.get("debtAmount"),allow_zero=True)!=0
        or _positive(u.get("bonus"),allow_zero=True)!=0
    ):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_COLLATERAL_SHAPE_NO_POST")
    return equity,available


def _single_country_contract(payload:object,symbol:str)->dict[str,Any]:
    if type(payload) is not dict or payload.get("success") is not True:
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_PUBLIC_CONTRACT_UNVERIFIED_NO_POST")
    rows=payload.get("data")
    rows=rows if type(rows) is list else [rows]
    hits=[r for r in rows if type(r) is dict and r.get("symbol")==symbol]
    if len(hits)!=1:
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_CONTRACT_NOT_UNIQUE_NO_POST")
    r=hits[0]
    if (
        r.get("apiAllowed") is not True
        or r.get("state")!=0
        or r.get("positionOpenType") not in (2,3)
        or r.get("futureType")!=1
        or r.get("settleCoin")!="USDT"
        or r.get("stopOnlyFair") is not False
    ):
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_CROSS_STOP_CONTRACT_UNAVAILABLE_NO_POST")
    return r


def _private_short_cross(rows:object,symbol:str,*,key:str)->dict[str,Any]:
    if key=="leverage":
        found=rows if type(rows) is list else []
    elif key=="risk":
        found=rows.get(symbol) if type(rows) is dict else None
        found=found if type(found) is list else []
    else:
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_PRIVATE_TIER_INVALID_NO_POST")
    matches=[r for r in found if type(r) is dict
             and r.get("positionType")==2 and r.get("openType")==2
             and r.get("symbol",symbol)==symbol]
    if len(matches)!=1:
        raise V40PortfolioPostHalted("V40_FOUR_SLOT_SHORT_CROSS_TIER_NOT_UNIQUE_NO_POST")
    return matches[0]


class V40FourSlotPostGuard:
    """Signed and DB-backed last-moment gate; no mutation method exists."""

    def __init__(
        self,*,ledger:PostgresFourSlotLedgerReader,
        client:Any,public_get:Any,
        flat_guard:PostgresV40GuardianPostGate,
        owned_guard:PostgresV40NonflatGuardianEvidence,
    )->None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or not callable(getattr(client,"_private_get",None))
            or not callable(getattr(public_get,"get",None))
            or type(flat_guard) is not PostgresV40GuardianPostGate
            or type(owned_guard) is not PostgresV40NonflatGuardianEvidence
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_POST_GUARD_DEPENDENCY_INVALID")
        self.ledger=ledger
        self.client=client
        self.public=public_get
        self.flat_guard=flat_guard
        self.owned_guard=owned_guard

    async def _snapshot(self)->FourSlotSnapshotResult:
        snap=await inspect_existing_four_slots_get_only(
            ledger=self.ledger,client=self.client,
        )
        if type(snap) is not FourSlotSnapshotResult:
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_SNAPSHOT_TYPE_INVALID_NO_POST")
        return snap

    async def _guardian(self,occupied:int)->None:
        if occupied==0:
            g=await self.flat_guard.verify()
            if not (
                type(g) is V40GuardianPostGateEvidence
                and g.status==FLAT_OK
                and g.state_verified is True and g.outbox_verified is True
                and g.guardian_fresh is True and g.critical_latch_absent is True
                and g.entry_allowed is False and g.live_authorization is False
                and g.mexc_posts==0 and g.database_writes==0
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_FLAT_GUARDIAN_LATCHED_NO_POST")
        else:
            g=await self.owned_guard.verify()
            if not (
                type(g) is V40NonflatGuardianEvidence
                and g.status==NONFLAT_OK
                and g.guardian_protected_observation_fresh is True
                and g.no_current_scope_critical_or_pending_incidents is True
                and g.next_entry_allowed is False and g.live_authorization is False
                and g.exchange_posts==0 and g.database_writes==0
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_NONFLAT_GUARDIAN_LATCHED_NO_POST")

    @staticmethod
    def _assert_snapshot(
        snap:FourSlotSnapshotResult,ctx:V40PortfolioPostContext,
        *,symbol:str,
    )->None:
        expected_status=(
            "FLAT_OBSERVED_NONAUTHORIZING" if ctx.expected_occupied_slots==0
            else "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        )
        owned=(
            tuple(p.position_id for p in snap.recovery.recovered)
            if snap.recovery is not None else ()
        )
        if (
            snap.status!=expected_status
            or snap.occupied_slots!=ctx.expected_occupied_slots
            or not snap.ledger_hash
            or not snap.critical_inventory_hash
            or set(owned)!=set(ctx.expected_owned_position_ids)
            or len(owned)!=len(ctx.expected_owned_position_ids)
            or snap.entry_allowed is not False or snap.live_authorization is not False
            or (snap.recovery is not None
                and any(p.symbol==symbol for p in snap.recovery.recovered))
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_ACCOUNT_OWNERSHIP_DRIFT_NO_POST")

    async def verify(
        self,*,intent:EntryIntent,permit:LiveMutationPermit,
        context:V40PortfolioPostContext,
    )->V40PortfolioPostReview:
        if (
            type(context) is not V40PortfolioPostContext
            or type(context.expected_occupied_slots) is not int
            or not 0<=context.expected_occupied_slots<=3
            or type(context.expected_owned_position_ids) is not tuple
            or len(context.expected_owned_position_ids)!=context.expected_occupied_slots
            or len(set(context.expected_owned_position_ids))!=context.expected_occupied_slots
            or any(type(x) is not str or re.fullmatch(r"[1-9][0-9]*",x) is None
                   for x in context.expected_owned_position_ids)
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_EXPECTED_SLOTS_INVALID_NO_POST")
        try:
            first=await self._snapshot()
            self._assert_snapshot(first,context,symbol=intent.symbol)
            await self._guardian(context.expected_occupied_slots)
            a1=await self.client._private_get("/api/v1/private/account/assets")
            lev=await self.client._private_get(
                "/api/v1/private/position/leverage",
                {"symbol":intent.symbol},
            )
            tier=await self.client._private_get(
                "/api/v1/private/account/risk_limit",
                {"symbol":intent.symbol},
            )
            res=await self.public.get(
                "/api/v1/contract/detail/country",
                params={"symbol":intent.symbol},
            )
            res.raise_for_status()
            contract=_single_country_contract(res.json(),intent.symbol)
            leverage_row=_private_short_cross(lev,intent.symbol,key="leverage")
            risk_row=_private_short_cross(tier,intent.symbol,key="risk")
            equity,available=_asset_usdt(a1)
            proposed=permit.leverage
            if any(
                type(v) is not int or v<proposed
                for v in (
                    leverage_row.get("maxLeverageView"),
                    risk_row.get("maxLeverage"),
                    contract.get("maxLeverage"),
                )
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_LEVERAGE_LIMIT_NO_POST")
            country=contract.get("countryConfigContractMaxLeverage")
            if type(country) is not int or (country>0 and proposed>country):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_COUNTRY_LEVERAGE_LIMIT_NO_POST")
            vol=_positive(intent.volume_contracts)
            step=_positive(contract.get("volUnit"))
            tick=_positive(contract.get("priceUnit"))
            size=_positive(contract.get("contractSize"))
            exact_notional=intent.reference_price*vol*size
            exact_sl75=(
                (intent.reference_price*D("1.75")/tick)
                .to_integral_value(rounding="ROUND_CEILING")*tick
            )
            if (
                exact_notional!=intent.notional_usdt
                or intent.stop_price!=exact_sl75
            ):
                raise V40PortfolioPostHalted(
                    "V40_FOUR_SLOT_CONTRACT_NOTIONAL_OR_SL75_DRIFT_NO_POST"
                )
            if (
                vol<_positive(contract.get("minVol"))
                or vol>_positive(contract.get("maxVol"))
                or vol%step!=0
                or intent.stop_price%tick!=0
                or intent.reference_price%tick!=0
                or vol>_positive(risk_row.get("maxVol"))
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_LOT_TICK_OR_TIER_NO_POST")
            imr=_positive(risk_row.get("imr"),allow_zero=True)
            mmr=_positive(risk_row.get("mmr"))
            if imr>1 or mmr>=1:
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_RISK_TIER_BOUNDS_NO_POST")
            needed=max(intent.notional_usdt/D(proposed),intent.notional_usdt*imr)
            if (
                equity!=context.frozen_equity_at_plan_usdt
                or equity!=context.leverage_plan.observed_equity_usdt
                or needed>available
                  -context.fee_reserve_usdt
                  -context.protected_remaining_margin_headroom_usdt
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_CURRENT_CROSS_MARGIN_INSUFFICIENT")
            a2=await self.client._private_get("/api/v1/private/account/assets")
            end=await self._snapshot()
            self._assert_snapshot(end,context,symbol=intent.symbol)
            await self._guardian(context.expected_occupied_slots)
            second_equity,second_available=_asset_usdt(a2)
            if (
                first.ledger_hash!=end.ledger_hash
                or first.critical_inventory_hash!=end.critical_inventory_hash
                or (equity,available)!=(second_equity,second_available)
            ):
                raise V40PortfolioPostHalted("V40_FOUR_SLOT_FINAL_BOOKEND_CHANGED_NO_POST")
            return V40PortfolioPostReview(
                "V40_FOUR_SLOT_POST_GUARD_CURRENT_CROSS_AND_NATIVE_SL_VERIFIED",
                occupied_slots=context.expected_occupied_slots,
                selected_exchange_cross_leverage=proposed,
                signed_equity_usdt=equity,
                signed_available_margin_usdt=available,
                account_stop_and_ledger_bookends_verified=True,
                guardian_double_verified=True,
                private_margin_tier_verified=True,
                contract_exact_lot_and_stop_verified=True,
            )
        except V40PortfolioPostHalted:
            raise
        except BaseException:
            raise V40PortfolioPostHalted(
                "V40_FOUR_SLOT_CURRENT_GET_OR_GUARDIAN_FAILED_NO_POST"
            ) from None


class V40FourSlotMexcOrderTransport:
    """Actual single MEXC POST transport, gated by independent full scope."""

    open_type=2

    def __init__(
        self,*,client:Any,guard:V40FourSlotPostGuard,
        context:V40PortfolioPostContext,
    )->None:
        if (
            type(guard) is not V40FourSlotPostGuard
            or guard.client is not client
            or type(context) is not V40PortfolioPostContext
            or not callable(getattr(client,"_private_post",None))
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_TRANSPORT_BINDING_INVALID")
        self.client=client
        self.guard=guard
        self.context=context
        # Defense in depth. The persistent one-shot dispatch claim is the
        # restart-safe dedupe; this prevents reuse of one adapter instance.
        self._post_attempted=False

    async def submit_short_with_stop(
        self,intent:EntryIntent,*,permit:LiveMutationPermit,
    )->str:
        # Existing immutable release constant (NOT_CERTIFIED), adapter
        # certification and native-stop gates are checked here, BEFORE GET.
        try:
            checked=require_certified_transport_permit(permit)
        except Exception:
            raise V40PortfolioPostHalted(
                "V40_FOUR_SLOT_TYPED_PORTFOLIO_PERMIT_NOT_CERTIFIED_NO_POST"
            ) from None
        if type(intent) is not EntryIntent or checked.scope!=V40_PORTFOLIO_SCOPE:
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_SCOPE_OR_INTENT_INVALID_NO_POST")
        p=self.context.leverage_plan
        if (
            type(p) is not V40CrossLeveragePlan or p.status!=STATUS_FEASIBLE
            or checked.signal_id!=intent.signal_id
            or checked.symbol!=intent.symbol
            or checked.leverage!=p.selected_exchange_cross_leverage
            or not 1<=checked.leverage<=5
            or type(intent.signal_id) is not int or intent.signal_id<=0
            or type(intent.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT",intent.symbol) is None
            or intent.symbol in ("BTC_USDT","ETH_USDT")
            or type(intent.external_oid) is not str
            or intent.external_oid!=deterministic_t100_entry_oid(
                intent.signal_id,intent.symbol
            )
            or any(type(v) is not D or not v.is_finite() or v<=0
                   for v in (
                       intent.reference_price,intent.notional_usdt,
                       intent.volume_contracts,intent.stop_price,
                   ))
            or intent.stop_price<intent.reference_price*D("1.75")
            or intent.notional_usdt!=p.exact_order_notional_usdt
            or intent.notional_usdt!=self.context.frozen_order_notional_usdt
            or p.portfolio_gross_after_entry_usdt
                !=self.context.frozen_portfolio_gross_after_entry_usdt
            or p.observed_equity_usdt!=self.context.frozen_equity_at_plan_usdt
            or p.account_observations_verified is not True
            or p.nominal_initial_margin_fits is not True
            or p.frozen_strategy_notional_unchanged is not True
            or p.liquidation_or_stop_survival_certified is not False
            or checked.max_position_notional_usdt<intent.notional_usdt
            or checked.max_total_notional_usdt<
                self.context.frozen_portfolio_gross_after_entry_usdt
            or any(
                type(v) is not D or not v.is_finite() or v<0
                for v in (
                    self.context.protected_remaining_margin_headroom_usdt,
                    self.context.fee_reserve_usdt,
                )
            )
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_EXACT_STRATEGY_SIZE_OR_STOP_INVALID_NO_POST")
        review=await self.guard.verify(
            intent=intent,permit=checked,context=self.context,
        )
        if not (
            type(review) is V40PortfolioPostReview
            and review.status=="V40_FOUR_SLOT_POST_GUARD_CURRENT_CROSS_AND_NATIVE_SL_VERIFIED"
            and review.occupied_slots==self.context.expected_occupied_slots
            and review.selected_exchange_cross_leverage==checked.leverage
            and review.account_stop_and_ledger_bookends_verified is True
            and review.guardian_double_verified is True
            and review.private_margin_tier_verified is True
            and review.contract_exact_lot_and_stop_verified is True
            and review.live_authorization is False
            and review.entry_allowed is False
            and review.mexc_posts==review.database_writes==0
        ):
            raise V40PortfolioPostHalted("V40_FOUR_SLOT_POST_GUARD_UNVERIFIED_NO_POST")
        if self._post_attempted:
            raise V40PortfolioPostHalted(
                "V40_FOUR_SLOT_ALREADY_POSTED_RECONCILE_NO_RETRY"
            )
        self._post_attempted=True
        # One irreversible market SHORT order with attached SL75.
        # The caller already durably committed SUBMITTING and must reconcile
        # lost HTTP/ACK by exact externalOid instead of re-sending.
        try:
            response=await self.client._private_post(POST_PATH,{
                "symbol":intent.symbol,
                "price":format(intent.reference_price,"f"),
                "vol":format(intent.volume_contracts,"f"),
                "leverage":checked.leverage,
                "side":3,"type":5,"openType":2,"positionMode":1,
                "externalOid":intent.external_oid,
                "stopLossPrice":format(intent.stop_price,"f"),
                "lossTrend":1,
            })
        except BaseException:
            raise V40PortfolioPostHalted(
                "V40_FOUR_SLOT_POST_OUTCOME_UNKNOWN_RECONCILE_NO_RETRY"
            ) from None
        raw=response.get("orderId") if type(response) is dict else response
        if type(raw) not in (int,str) or re.fullmatch(r"[1-9][0-9]*",str(raw)) is None:
            raise V40PortfolioPostHalted(
                "V40_FOUR_SLOT_POST_ACK_AMBIGUOUS_RECONCILE_NO_RETRY"
            )
        return str(raw)
