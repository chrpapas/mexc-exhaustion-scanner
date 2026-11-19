"""Actual 1–3 owned CROSS-short MEXC candidate and executable bid-depth GET.

Unlike the first-canary flat-account market witness, this reader refuses to
treat existing authenticated V40-owned positions as unexpected exposure. It
requires the four-slot ledger/native-stop recovery, an independent same-signer
fair-price/current-tier assessment, a fresh hedge/CROSS settings GET for the
candidate, account equity/available margin bookends, actual key order-write
permission, complete UTC-day closed-position history and a fresh sized MEXC
depth book. It does NOT claim future liquidation survival, write a trade,
grant a permit, or substitute the four-slot frozen allocation algorithm.

The returned "cross_collateral_verified" refers strictly to current observed
signed available balance and already-owned protected account economics, NOT
a guaranteed futures cross wallet or 75%-gap liquidation outcome.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal as D
from typing import Any, Awaitable, Callable

from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_v40_stage2_source import V40SourceCandidate
from app.t100_live_v40_dispatch_executor import V40CurrentRiskEvidence
from app.t100_live_v40_entry_coordinator import V40ContractTerms
from app.t100_live_v40_candidate_market_witness import (
    V40CandidateQuote,V40ExactDepth,_decimal,_level_rows,
)
from app.t100_live_execution import EntryIntent,displayed_vwap
from app.t100_v40_canary_daily_loss import V40DailyLossEvidence
from app.t100_v40_canary_key_permissions import V40KeyPermissionObservation
from app.t100_v40_unreleased_same_signer_risk_reader import V40SameSignerCurrentRiskReader
from app.t100_v40_unreleased_current_portfolio_risk import V40PortfolioCurrentRisk


class V40OwnedCrossCandidateHalted(RuntimeError):
    """No raw MEXC, SQL or secret values in exception messages."""


class V40OwnedCrossCandidateWitness:
    def __init__(
        self, *,
        ledger:PostgresFourSlotLedgerReader,
        snapshots:FourSlotSnapshotCoordinator,
        venue:FourSlotMexcReadVenue,
        private:ReadOnlyMexcProbe,
        public:Any,
        same_signer_risk:V40SameSignerCurrentRiskReader,
        read_key_permissions:Callable[[],Awaitable[V40KeyPermissionObservation]],
        read_daily_loss:Callable[[],Awaitable[V40DailyLossEvidence]],
        clock_ms:Callable[[],int],
        max_book_age_ms:int=1500,
    )->None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or type(snapshots) is not FourSlotSnapshotCoordinator
            or type(venue) is not FourSlotMexcReadVenue
            or type(private) is not ReadOnlyMexcProbe
            or venue.probe is not private
            or snapshots.ledger is not ledger
            or snapshots.venue is not venue
            or type(same_signer_risk) is not V40SameSignerCurrentRiskReader
            or same_signer_risk.ledger is not ledger
            or same_signer_risk.private is not private
            or same_signer_risk.public is not public
            or not callable(getattr(public,"get",None))
            or not callable(read_key_permissions)
            or not callable(read_daily_loss)
            or not callable(clock_ms)
            or type(max_book_age_ms) is not int
            or not 0<max_book_age_ms<=2000
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_SAME_ACCOUNT_READERS_AND_WRITER_SCOPE_REQUIRED"
            )
        self.ledger=ledger
        self.snapshots=snapshots
        self.venue=venue
        self.private=private
        self.public=public
        self.same_signer_risk=same_signer_risk
        self.read_key_permissions=read_key_permissions
        self.read_daily_loss=read_daily_loss
        self.clock_ms=clock_ms
        self.max_book_age_ms=max_book_age_ms

    async def _public(self,path:str,params:dict[str,str]|None)->dict[str,Any]:
        try:
            response=await self.public.get(path,params=params)
            response.raise_for_status()
            raw=response.json()
            if (
                type(raw) is not dict
                or raw.get("success") is not True
                or raw.get("code")!=0
                or type(raw.get("data")) is not dict
            ):
                raise ValueError()
            return raw["data"]
        except BaseException:
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_PUBLIC_CONTRACT_OR_DEPTH_GET_FAILED"
            ) from None

    @staticmethod
    def _usdt_assets(assets:Any)->tuple[D,D,str]:
        if (
            type(assets) is not list
            or len([r for r in assets
                    if type(r) is dict and r.get("currency")=="USDT"])!=1
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_USDT_WALLET_NOT_UNIQUE"
            )
        row=next(r for r in assets if r.get("currency")=="USDT")
        try:
            equity=_decimal(row.get("equity"))
            available=_decimal(row.get("availableBalance"),positive=False)
            # Current "availableBalance" is not a guaranteed cross wallet,
            # but it IS the signed, currently available initial-margin sample.
            if available>equity+D("0.00000001"):
                raise ValueError()
            # Require ALL collateral fields unchanged in a double GET.
            import json
            economy=json.dumps(
                {k:row.get(k) for k in (
                    "equity","availableBalance","cashBalance","frozenBalance",
                    "positionMargin","unrealized","debtAmount",
                    "contributeMarginAmount","bonus",
                )},
                sort_keys=True,separators=(",",":"),allow_nan=False,
            )
        except (ValueError,TypeError,ArithmeticError):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_ASSET_ECONOMICS_INVALID"
            ) from None
        return equity,min(available,equity),economy

    async def _capture(self,signal:V40SourceCandidate):
        if (
            type(signal) is not V40SourceCandidate
            or signal.entry_allowed is not False
            or signal.live_authorization is not False
            or not signal.symbol.endswith("_USDT")
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_GENUINE_SIGNAL_REQUIRED"
            )
        start=self.clock_ms()
        first=await self.snapshots.inspect()
        if (
            first.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
            or not 1<=first.occupied_slots<=3
            or first.recovery is None
            or not first.ledger_hash or not first.critical_inventory_hash
            or signal.symbol in {p.symbol for p in first.recovery.recovered}
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_ONE_TO_THREE_PROTECTED_SLOTS_REQUIRED"
            )
        # Its entire 1..3-slot current risk uses the very same signer.
        owned=await self.same_signer_risk.observe_current()
        if (
            type(owned) is not V40PortfolioCurrentRisk
            or owned.status!="V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING"
            or owned.slots!=first.occupied_slots
            or owned.all_owned_positions_and_native_stops_verified is not True
            or owned.same_venue_inventory_in_both_readers is not True
            or owned.current_private_public_tiers_verified is not True
            or owned.entry_allowed is not False
            or owned.live_authorization is not False
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_EXISTING_MARGIN_AND_STOPS_NOT_VERIFIED"
            )
        # Independent candidate settings, not guessed from owned symbols.
        try:
            mode=await self.private.raw_position_mode()
            assets=await self.private.raw_account_assets()
            lev=await self.private.raw_position_leverage(signal.symbol)
            tiers=await self.private.raw_account_risk_limits(signal.symbol)
            contract=await self._public(
                "/api/v1/contract/detail/country",{"symbol":signal.symbol},
            )
            depth=await self._public(
                "/api/v1/contract/depth/"+signal.symbol,{"limit":"100"},
            )
            again=await self.private.raw_account_assets()
            second=await self.snapshots.inspect()
            end=self.clock_ms()
            if (
                type(start) is not int or type(end) is not int
                or start<=0 or end<start or end-start>15000
                or mode!=1
                or second.status!=first.status
                or second.occupied_slots!=first.occupied_slots
                or second.ledger_hash!=first.ledger_hash
                or second.critical_inventory_hash!=first.critical_inventory_hash
            ):
                raise ValueError()
            equity,available,cash=self._usdt_assets(assets)
            end_equity,end_available,end_cash=self._usdt_assets(again)
            if (equity,available,cash)!=(end_equity,end_available,end_cash):
                raise ValueError()
            if abs(owned.observed_account_equity_usdt-equity)>D("0.00000001"):
                raise ValueError()
            if (
                type(lev) is not list or not 1<=len(lev)<=2
                or len([r for r in lev
                        if type(r) is dict and r.get("positionType")==2])!=1
            ):
                raise ValueError()
            config=next(r for r in lev if r["positionType"]==2)
            if (
                config.get("openType")!=2
                or type(config.get("leverage")) is not int
                or not 1<=config["leverage"]<=5
                or type(tiers) is not dict
                or set(tiers)!={signal.symbol}
            ):
                raise ValueError()
            shorts=[r for r in tiers[signal.symbol]
                    if type(r) is dict and r.get("positionType")==2]
            if len(shorts)!=1:
                raise ValueError()
            risk=shorts[0]
            if (
                risk.get("symbol")!=signal.symbol
                or type(risk.get("level")) is not int or risk["level"]<1
                or ("openType" in risk and risk["openType"]!=2)
                or ("leverage" in risk and risk["leverage"]!=config["leverage"])
            ):
                raise ValueError()
            private_max=_decimal(risk.get("maxVol"))
            if (
                contract.get("symbol")!=signal.symbol
                or contract.get("apiAllowed") is not True
                or contract.get("state")!=0
                or contract.get("quoteCoin")!="USDT"
                or contract.get("settleCoin")!="USDT"
                or contract.get("futureType")!=1
                or contract.get("positionOpenType") not in (2,3)
            ):
                raise ValueError()
            tick=_decimal(contract.get("priceUnit"))
            volunit=_decimal(contract.get("volUnit"))
            minvol=_decimal(contract.get("minVol"))
            maxvol=_decimal(contract.get("maxVol"))
            size=_decimal(contract.get("contractSize"))
            if minvol>maxvol or private_max<minvol:
                raise ValueError()
            if (
                type(depth.get("timestamp")) is not int
                or type(depth.get("version")) is not int
                or depth["version"]<=0
                or depth["timestamp"]>end+100
                or not 0<=end-depth["timestamp"]<=self.max_book_age_ms
            ):
                raise ValueError()
            bids=_level_rows(depth.get("bids"),side="sell")
            asks=_level_rows(depth.get("asks"),side="buy")
            if bids[0][0]>=asks[0][0]:
                raise ValueError()
        except BaseException:
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_CANDIDATE_CONFIG_OR_ACCOUNT_DRIFT"
            ) from None
        key=await self.read_key_permissions()
        daily=await self.read_daily_loss()
        if (
            type(key) is not V40KeyPermissionObservation
            or key.status!="V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
            or key.api_key_identity_verified is not True
            or key.api_key_active_verified is not True
            or key.futures_account_read_scope_verified is not True
            or key.futures_order_read_scope_verified is not True
            or key.futures_order_write_scope_verified is not True
            or key.entry_allowed is not False
            or key.mexc_posts!=0
            or type(daily) is not V40DailyLossEvidence
            or daily.status!="V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING"
            or daily.complete_two_passes is not True
            or daily.complete_history_verified is not True
            or daily.realized_today_loss_usdt is None
            or daily.mexc_posts!=0 or daily.database_writes!=0
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_API_KEY_OR_DAILY_PNL_NOT_VERIFIED"
            )
        try:
            daily_loss=_decimal(daily.realized_today_loss_usdt,positive=False)
        except Exception:
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_DAILY_LOSS_NUMERIC_INVALID"
            ) from None
        # These are the current *observed* MEXC economic facts; this
        # doesn't certify stressed 75% liquidation or guarantee SL fills.
        terms=V40ContractTerms(
            contract_size=size,volume_step=volunit,
            min_volume=minvol,max_volume=maxvol,price_tick=tick,
            private_order_permission_verified=True,
            market_state_and_country_verified=True,
        )
        quote=V40CandidateQuote(
            status="V40_GET_ONLY_OWNED_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
            symbol=signal.symbol,observed_at_ms=end,
            book_age_ms=end-depth["timestamp"],
            account_equity_usdt=equity,account_available_usdt=available,
            candidate_contract=terms,
            current_private_short_max_vol=private_max,
            top_bid=bids[0][0],top_ask=asks[0][0],
            bookended_flat_account=False,hedge_mode_observed=True,
            private_risk_row_observed=True,
            # Newly-added strict owned-mode check in sized-depth gate.
            owned_protected_positions_bookended=first.occupied_slots,
            full_cross_collateral_verified=False,
            complete_realized_daily_loss_verified=True,
            private_key_order_permission_verified=True,
        )
        r=V40CurrentRiskEvidence(
            current_account_equity_usdt=equity,
            available_margin_usdt=available,
            portfolio_notional_usdt=owned.current_gross_fair_notional_usdt,
            realized_today_loss_usdt=daily_loss,
            current_owned_positions=first.occupied_slots,
            signal_age_seconds=max(
                0.,(end-signal.admission_open_ts*1000)/1000.,
            ),
            book_age_ms=quote.book_age_ms,
            observed_sell_vwap=D(0),
            inventory_bookended_complete=True,
            no_unowned_positions_or_orders=True,
            cross_collateral_verified=True,  # current signed available only
            private_risk_tier_verified=True,  # current tier only
            contract_trading_access_verified=True, # key+public contract
            hedge_mode_verified=True,
            no_unresolved_mutations=True,
        )
        return quote,depth,r

    async def inspect(
        self,signal:V40SourceCandidate,
    )->tuple[V40CurrentRiskEvidence,V40ContractTerms]:
        q,_,r=await self._capture(signal)
        return r,q.candidate_contract

    async def quote_exact_intent(
        self,signal:V40SourceCandidate,intent:EntryIntent,*,
        max_slippage_bps:D,
    )->tuple[V40CandidateQuote,V40ExactDepth]:
        if (
            type(intent) is not EntryIntent
            or intent.symbol!=signal.symbol
            or intent.signal_id!=signal.signal_id
            or type(max_slippage_bps) is not D
            or not D(0)<=max_slippage_bps<=D(300)
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_SIZED_QUOTE_INPUT_INVALID"
            )
        quote,book,_=await self._capture(signal)
        bids=_level_rows(book.get("bids"),side="sell")
        try:
            vwap,filled=displayed_vwap(
                levels=bids,contract_size=quote.candidate_contract.contract_size,
                requested_contracts=intent.volume_contracts,side="sell",
            )
            slip=(intent.reference_price-vwap)/intent.reference_price*D(10000)
        except Exception:
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_DISPLAYED_BID_DEPTH_UNVERIFIED"
            ) from None
        if (
            filled!=intent.volume_contracts
            or vwap<=0 or slip>max_slippage_bps
            or intent.volume_contracts>quote.current_private_short_max_vol
            or intent.volume_contracts>quote.candidate_contract.max_volume
            or intent.volume_contracts<quote.candidate_contract.min_volume
            or intent.volume_contracts%quote.candidate_contract.volume_step!=0
            or intent.notional_usdt!=(
                intent.reference_price*intent.volume_contracts
                *quote.candidate_contract.contract_size
            )
        ):
            raise V40OwnedCrossCandidateHalted(
                "V40_OWNED_CROSS_UNEXECUTABLE_LOT_OR_SLIPPAGE"
            )
        return quote,V40ExactDepth(
            status="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING",
            symbol=signal.symbol,signal_id=signal.signal_id,
            contracts=intent.volume_contracts,sell_vwap=vwap,
            adverse_slippage_bps=slip,
            age_ms=quote.book_age_ms,order_fits_depth=True,
        )
