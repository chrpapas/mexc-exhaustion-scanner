"""Real signed-GET/public-GET candidate-market and FLAT-account witness.

Never certifies production dispatch: account cross-collateral semantics,
daily realized-loss completeness, private API-key order permission and
exchange stop-market lifecycle need independent evidence. The returned
V40CurrentRiskEvidence intentionally keeps those flags FALSE.

Uses existing signed GET-only probe, fresh public country-scoped contract,
MEXC order-book depth for the exact rounded executable lot quantity, exact
hedge-mode GET, repeated stable full-account inventory and USDT economic
reconciliation. This is for ONE isolated FLAT subaccount canary; it neither
adopts existing positions nor infers them to be T100-owned.

No POST, SQL mutation, authorization, account configuration, logging of raw
venue payloads or secrets.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any, Callable, Protocol

from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS,
)
from app.t100_live_execution import EntryIntent, displayed_vwap
from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_v40_dispatch_executor import V40CurrentRiskEvidence
from app.t100_live_v40_entry_coordinator import V40ContractTerms
from app.t100_live_v40_stage2_source import V40SourceCandidate

_COUNTRY_PATH="/api/v1/contract/detail/country"
_DEPTH_PREFIX="/api/v1/contract/depth/"
_MODE_PATH="/api/v1/private/position/position_mode"


class V40CandidateMarketHalted(RuntimeError):
    """Only fixed failure identifiers, never account/URL/secret values."""


class PublicGetClient(Protocol):
    async def get(self, path: str, *, params: dict[str,str]|None=None) -> Any: ...


def _decimal(value: Any, *, positive: bool=True) -> D:
    if value is None or type(value) is bool:
        raise V40CandidateMarketHalted("MARKET_INVALID_DECIMAL")
    try:
        n=D(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise V40CandidateMarketHalted("MARKET_INVALID_DECIMAL") from None
    if not n.is_finite() or (n<=0 if positive else n<0):
        raise V40CandidateMarketHalted("MARKET_INVALID_DECIMAL")
    return n


def _canonical(rows: Any, fields: tuple[str,...]) -> tuple[str,...]:
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise V40CandidateMarketHalted("MARKET_BOOKEND_ROWS_INVALID")
    try:
        return tuple(sorted(
            json.dumps(
                {field: row[field] for field in fields if field in row},
                sort_keys=True, separators=(",",":"),allow_nan=False,
            ) for row in rows
        ))
    except (ValueError, TypeError):
        raise V40CandidateMarketHalted("MARKET_BOOKEND_ENCODING_INVALID") from None


def _counts_empty(counts: Any) -> bool:
    return type(counts) is dict and all(
        type(counts.get(name)) is int and counts[name]==0
        for name in (
            "sumCount","limitOrderCount","stopOrderCount",
            "planOrderCount","trackOrderCount",
        )
    )


def _asset(assets: Any) -> tuple[D,D]:
    if type(assets) is not list:
        raise V40CandidateMarketHalted("MARKET_ASSET_COLLECTION_INVALID")
    report=assess_position_margin_witness([],assets)
    if report.get("position_inventory_economic_witness_observed") is not True:
        raise V40CandidateMarketHalted("MARKET_FLAT_ASSET_ECONOMICS_UNVERIFIED")
    rows=[row for row in assets if type(row) is dict and row.get("currency")=="USDT"]
    if len(rows)!=1:
        raise V40CandidateMarketHalted("MARKET_USDT_ASSET_NOT_UNIQUE")
    equity=_decimal(rows[0].get("equity"))
    available=_decimal(rows[0].get("availableBalance"),positive=False)
    if available>equity:
        # MEXC's official account/assets example reports
        # 32.80984793658 available and 32.80984793 equity, with no
        # positions/margin. Admit ONLY a <=1e-8 USDT precision delta
        # if the cash/equity readings agree. Never use the excess
        # precision as spendable collateral or certify cross margin.
        gap=available-equity
        cash=_decimal(rows[0].get("cashBalance"))
        if gap>D("0.00000001") or abs(cash-equity)>D("0.00000001"):
            raise V40CandidateMarketHalted("MARKET_USDT_COLLATERAL_DRIFT")
        available=equity
    if any(
        _decimal(rows[0].get(field),positive=False)!=0
        for field in ("positionMargin","unrealized")
    ):
        raise V40CandidateMarketHalted("MARKET_USDT_COLLATERAL_DRIFT")
    return equity,available


def _level_rows(rows: Any, *, side: str) -> list[list[D]]:
    if type(rows) is not list or not 1<=len(rows)<=200:
        raise V40CandidateMarketHalted("MARKET_DEPTH_LEVELS_INVALID")
    clean=[]
    for row in rows:
        if type(row) is not list or len(row)<2:
            raise V40CandidateMarketHalted("MARKET_DEPTH_ROW_INVALID")
        price=_decimal(row[0])
        qty=_decimal(row[1])
        clean.append([price,qty])
    prices=[row[0] for row in clean]
    if len(set(prices))!=len(prices) or any(
        (prices[i]<=prices[i+1] if side=="sell" else prices[i]>=prices[i+1])
        for i in range(len(prices)-1)
    ):
        raise V40CandidateMarketHalted("MARKET_DEPTH_NOT_STRICTLY_SORTED")
    return clean


@dataclass(frozen=True)
class V40CandidateQuote:
    status: str
    symbol: str
    observed_at_ms: int
    book_age_ms: int
    account_equity_usdt: D
    account_available_usdt: D
    candidate_contract: V40ContractTerms
    current_private_short_max_vol: D
    # A preflight quote of one nominal unit is NOT sufficient for a
    # quantity-specific market order. Use quote_exact_intent().
    top_bid: D
    top_ask: D
    # These are assertions the actual reader can verify for a FLAT account.
    bookended_flat_account: bool
    hedge_mode_observed: bool
    private_risk_row_observed: bool
    # These are NOT obtained from current asset/risk endpoints.
    full_cross_collateral_verified: bool=False
    complete_realized_daily_loss_verified: bool=False
    private_key_order_permission_verified: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False


@dataclass(frozen=True)
class V40ExactDepth:
    status: str
    symbol: str
    signal_id: int
    contracts: D
    sell_vwap: D
    adverse_slippage_bps: D
    age_ms: int
    order_fits_depth: bool
    entry_allowed: bool=False
    live_authorization: bool=False


class MexcV40CandidateMarketWitness:
    """Read-only current-market source; never issues a live mutation permit."""

    def __init__(
        self, *, private: ReadOnlyMexcProbe,
        public: PublicGetClient,
        clock_ms: Callable[[],int],
        max_book_age_ms: int=1500,
        max_cycle_ms: int=5000,
    ) -> None:
        if (
            type(private) is not ReadOnlyMexcProbe
            or public is None or not callable(clock_ms)
            or type(max_book_age_ms) is not int
            or not 0<max_book_age_ms<=2000
            or type(max_cycle_ms) is not int
            or not 0<max_cycle_ms<=5000
        ):
            raise ValueError("V40_MARKET_WITNESS_CONFIG_INVALID")
        self.private=private
        self.public=public
        self.clock_ms=clock_ms
        self.max_book_age_ms=max_book_age_ms
        self.max_cycle_ms=max_cycle_ms

    async def _public_data(
        self, path: str, params: dict[str,str]|None=None,
    ) -> dict[str,Any]:
        try:
            response=await self.public.get(path,params=params)
            response.raise_for_status()
            raw=response.json()
        except Exception:
            raise V40CandidateMarketHalted("MARKET_PUBLIC_GET_FAILED") from None
        if (
            type(raw) is not dict or raw.get("success") is not True
            or raw.get("code")!=0 or type(raw.get("data")) is not dict
        ):
            raise V40CandidateMarketHalted("MARKET_PUBLIC_ENVELOPE_INVALID")
        return raw["data"]

    async def _account(self) -> tuple[list[dict[str,Any]],list[dict[str,Any]],dict[str,int],list[dict[str,Any]],int]:
        positions=await self.private.raw_open_positions()
        stops=await self.private.raw_open_stops()
        counts=await self.private.raw_open_order_counts()
        assets=await self.private.raw_account_assets()
        # Current MEXC docs say positionMode=1 is hedge; this is a
        # PRIVATE GET not an assumed default of order/create.
        mode=await self.private.raw_position_mode()
        return positions,stops,counts,assets,mode

    async def _observe(
        self, signal: V40SourceCandidate,
    ) -> tuple[V40CandidateQuote,dict[str,Any]]:
        if (
            type(signal) is not V40SourceCandidate
            or not re.fullmatch(r"[A-Z0-9]+_USDT",signal.symbol)
            or signal.symbol in ("BTC_USDT","ETH_USDT")
        ):
            raise V40CandidateMarketHalted("MARKET_SIGNAL_IDENTITY_INVALID")
        started=self.clock_ms()
        if type(started) is not int or started<=0:
            raise V40CandidateMarketHalted("MARKET_CLOCK_INVALID")
        try:
            before=await self._account()
            public=await self._public_data(
                _COUNTRY_PATH,{"symbol":signal.symbol},
            )
            risks=await self.private.raw_account_risk_limits(signal.symbol)
            depth=await self._public_data(
                _DEPTH_PREFIX+signal.symbol,{"limit":"100"},
            )
            after=await self._account()
        except V40CandidateMarketHalted:
            raise
        except Exception:
            raise V40CandidateMarketHalted("MARKET_SIGNED_GET_FAILED") from None
        ended=self.clock_ms()
        if (
            type(ended) is not int or ended<started
            or ended-started>self.max_cycle_ms
        ):
            raise V40CandidateMarketHalted("MARKET_TOTAL_CYCLE_STALE")
        for state in (before,after):
            if (
                state[0] or state[1] or not _counts_empty(state[2])
                or state[4]!=1
            ):
                raise V40CandidateMarketHalted("MARKET_NOT_FLAT_HEDGE_ACCOUNT")
        # Compare unchanged account economic identity as well as zero
        # positions and zero outstanding orders.
        if (
            _canonical(before[0],CRITICAL_POSITION_FIELDS)
            !=_canonical(after[0],CRITICAL_POSITION_FIELDS)
            or _canonical(before[1],CRITICAL_STOP_FIELDS)
            !=_canonical(after[1],CRITICAL_STOP_FIELDS)
            or before[2]!=after[2]
            or before[4]!=after[4]
        ):
            raise V40CandidateMarketHalted("MARKET_ACCOUNT_BOOKEND_DRIFT")
        equity,available=_asset(after[3])
        before_equity,before_available=_asset(before[3])
        if equity!=before_equity or available!=before_available:
            raise V40CandidateMarketHalted("MARKET_ASSET_BOOKEND_DRIFT")
        # A transfer or order-margin freeze might not appear in equity or
        # availableBalance immediately. Require all reported collateral
        # components to agree between signed GET bookends as well.
        cash_fields=(
            "cashBalance","frozenBalance","bonus","debtAmount",
            "contributeMarginAmount","positionMargin","unrealized",
        )
        for field in cash_fields:
            before_usdt=next(x for x in before[3]
                             if type(x) is dict and x.get("currency")=="USDT")
            after_usdt=next(x for x in after[3]
                            if type(x) is dict and x.get("currency")=="USDT")
            if field not in before_usdt or field not in after_usdt:
                raise V40CandidateMarketHalted("MARKET_COLLATERAL_COMPONENT_MISSING")
            if _decimal(before_usdt[field],positive=False) != _decimal(
                after_usdt[field],positive=False
            ):
                raise V40CandidateMarketHalted("MARKET_COLLATERAL_COMPONENT_DRIFT")
        if (
            public.get("symbol")!=signal.symbol
            or public.get("apiAllowed") is not True
            or type(public.get("state")) is not int or public["state"]!=0
            or public.get("quoteCoin")!="USDT"
            or public.get("settleCoin")!="USDT"
            or type(public.get("futureType")) is not int or public["futureType"]!=1
            or type(public.get("positionOpenType")) is not int
            or public["positionOpenType"] not in (2,3)
        ):
            raise V40CandidateMarketHalted("MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE")
        try:
            price_tick=_decimal(public.get("priceUnit"))
            size=_decimal(public.get("contractSize"))
            step=_decimal(public.get("volUnit"))
            minimum=_decimal(public.get("minVol"))
            maximum=_decimal(public.get("maxVol"))
        except V40CandidateMarketHalted:
            raise V40CandidateMarketHalted("MARKET_CONTRACT_SIZE_GRID_INVALID") from None
        if minimum>maximum or minimum%step!=0 or maximum%step!=0:
            raise V40CandidateMarketHalted("MARKET_CONTRACT_LOT_GRID_INVALID")
        if (
            type(risks) is not dict or set(risks)!={signal.symbol}
            or type(risks[signal.symbol]) is not list
        ):
            raise V40CandidateMarketHalted("MARKET_PRIVATE_RISK_SCOPE_INVALID")
        shorts=[row for row in risks[signal.symbol]
                if type(row) is dict and type(row.get("positionType")) is int
                and row["positionType"]==2]
        if len(shorts)!=1 or any(
            type(row) is not dict or row.get("symbol")!=signal.symbol
            for row in risks[signal.symbol]
        ):
            raise V40CandidateMarketHalted("MARKET_PRIVATE_SHORT_RISK_UNVERIFIED")
        short=shorts[0]
        if (
            type(short.get("openType")) is not int
            or short["openType"]!=2
            or type(short.get("leverage")) is not int
            or short["leverage"]!=1
        ):
            raise V40CandidateMarketHalted("MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED")
        private_max=_decimal(short.get("maxVol"))
        if private_max<minimum:
            raise V40CandidateMarketHalted("MARKET_PRIVATE_VOLUME_BELOW_MINIMUM")
        if (
            type(depth.get("timestamp")) is not int
            or depth["timestamp"]>ended+100
            or ended-depth["timestamp"]>self.max_book_age_ms
            or type(depth.get("version")) is not int
            or depth["version"]<=0
        ):
            raise V40CandidateMarketHalted("MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE")
        bids=_level_rows(depth.get("bids"),side="sell")
        asks=_level_rows(depth.get("asks"),side="buy")
        if bids[0][0]>=asks[0][0]:
            raise V40CandidateMarketHalted("MARKET_CROSSED_ORDER_BOOK")
        terms=V40ContractTerms(
            contract_size=size,volume_step=step,
            min_volume=minimum,max_volume=maximum,price_tick=price_tick,
            # Public apiAllowed does NOT establish this API key's POST
            # privilege. Remain false until separately verified.
            private_order_permission_verified=False,
            market_state_and_country_verified=True,
        )
        return V40CandidateQuote(
            status="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
            symbol=signal.symbol,observed_at_ms=ended,
            book_age_ms=ended-depth["timestamp"],
            account_equity_usdt=equity,account_available_usdt=available,
            candidate_contract=terms,
            current_private_short_max_vol=private_max,
            top_bid=bids[0][0],top_ask=asks[0][0],
            bookended_flat_account=True,hedge_mode_observed=True,
            private_risk_row_observed=True,
        ),depth

    async def observe(
        self, signal: V40SourceCandidate,
    ) -> V40CandidateQuote:
        quote,_=await self._observe(signal)
        return quote

    async def quote_exact_intent(
        self, signal: V40SourceCandidate, intent: EntryIntent,
        *, max_slippage_bps: D=D("100"),
    ) -> tuple[V40CandidateQuote,V40ExactDepth]:
        """Fresh book VWAP for the ACTUAL lot-quantized intended quantity."""
        if (
            type(intent) is not EntryIntent
            or intent.signal_id!=signal.signal_id
            or intent.symbol!=signal.symbol
            or type(max_slippage_bps) is not D
            or not max_slippage_bps.is_finite()
            or not D(0)<=max_slippage_bps<=D(300)
        ):
            raise V40CandidateMarketHalted("MARKET_EXECUTION_QUOTE_INPUT_INVALID")
        quote,depth=await self._observe(signal)
        # The venue has only a sampled, not guaranteed, spendable balance.
        # Nevertheless, a 1x order already exceeding that sample cannot
        # be marketed as a depth-executable candidate. Include a one-sided
        # conservative fee buffer even though the final risk gate will make
        # a separate collateral determination.
        if (
            intent.notional_usdt*D("1.02")>quote.account_available_usdt
        ):
            raise V40CandidateMarketHalted(
                "MARKET_ONE_X_MARGIN_WITH_FEE_BUFFER_INSUFFICIENT"
            )
        if (
            intent.volume_contracts>quote.candidate_contract.max_volume
            or intent.volume_contracts>quote.current_private_short_max_vol
            or intent.volume_contracts<quote.candidate_contract.min_volume
            or intent.volume_contracts%quote.candidate_contract.volume_step!=0
            or intent.notional_usdt!=(
                intent.reference_price*quote.candidate_contract.contract_size
                *intent.volume_contracts
            )
        ):
            raise V40CandidateMarketHalted("MARKET_EXECUTION_CONTRACT_OR_RISK_CAP")
        bids=_level_rows(depth["bids"],side="sell")
        try:
            vwap,filled=displayed_vwap(
                levels=bids,contract_size=quote.candidate_contract.contract_size,
                requested_contracts=intent.volume_contracts,side="sell",
            )
        except Exception:
            raise V40CandidateMarketHalted("MARKET_INSUFFICIENT_DISPLAYED_BIDS") from None
        adverse=(intent.reference_price-vwap)/intent.reference_price*D(10000)
        if (
            filled!=intent.volume_contracts
            or adverse>max_slippage_bps
            or vwap<=0
        ):
            raise V40CandidateMarketHalted("MARKET_DEPTH_SLIPPAGE_REJECTED")
        return quote,V40ExactDepth(
            status="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING",
            symbol=signal.symbol,signal_id=signal.signal_id,
            contracts=intent.volume_contracts,sell_vwap=vwap,
            adverse_slippage_bps=adverse,age_ms=quote.book_age_ms,
            order_fits_depth=True,
        )

    async def inspect(
        self, signal: V40SourceCandidate,
    ) -> tuple[V40CurrentRiskEvidence,V40ContractTerms]:
        """One-shot coordinator-compatible *nonauthorizing* evidence.

        Caller MUST NOT self-certify cross collateral, daily PnL or private
        POST permissions by changing flags based on this source alone.
        The one-shot coordinator's strict permit gate prevents DB mutation.
        """
        quote=await self.observe(signal)
        age=(self.clock_ms()-signal.admission_open_ts*1000)/1000.
        if age<0 or age>900:
            raise V40CandidateMarketHalted("MARKET_SIGNAL_TOO_OLD")
        risk=V40CurrentRiskEvidence(
            current_account_equity_usdt=quote.account_equity_usdt,
            available_margin_usdt=quote.account_available_usdt,
            portfolio_notional_usdt=D(0),
            # This is a harmless zero placeholder ONLY because the
            # unverified daily PnL flag below prevents authorization.
            realized_today_loss_usdt=D(0),
            current_owned_positions=0,
            signal_age_seconds=age,
            book_age_ms=quote.book_age_ms,
            # No sized order was supplied to inspect(); top-of-book is
            # not an executable-order VWAP. Zero deliberately cannot pass
            # the dispatch preclaim validation.
            observed_sell_vwap=D(0),
            inventory_bookended_complete=True,
            no_unowned_positions_or_orders=True,
            cross_collateral_verified=False,
            private_risk_tier_verified=False,
            contract_trading_access_verified=False,
            hedge_mode_verified=True,
            no_unresolved_mutations=False,
        )
        return risk,quote.candidate_contract
