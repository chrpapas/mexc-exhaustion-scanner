"""Strict flat-account first-V40-canary risk witness from real signed GETs.

For exactly ONE small CROSS 1x SHORT, combine independent current contract
and depth/collateral bookends, same-account API key WRITE proof, full
historical UTC-day realized-loss read, and restricted runtime journal SQL.
No value comes from a paper position. No POST, stop mutation or DB write.

This is intentionally NOT certification of future margin tiers, execution
atomicity, the native-stop fill path, or unattended portfolio trading. It
provides the verified current-only risk inputs the existing fenced
V40OneShotCoordinator requires. Other independently issued permit and
guardian/POST gates still control the actual exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
from datetime import datetime,timezone
from decimal import Decimal as D,InvalidOperation
from typing import Any,Awaitable,Callable

from app.t100_live_v40_entry_coordinator import V40ContractTerms
from app.t100_live_v40_dispatch_executor import V40CurrentRiskEvidence
from app.t100_live_v40_stage2_source import V40SourceCandidate
from app.t100_live_v40_candidate_market_witness import (
    V40CandidateQuote,MexcV40CandidateMarketWitness,
)
from app.t100_v40_canary_daily_loss import (
    V40DailyLossEvidence,V40HistoryReader,observe_daily_loss,
)
from app.t100_v40_canary_key_permissions import V40KeyPermissionObservation
from app.t100_v40_render_journal_readonly import V40RuntimeJournalObservation

_MAX_NOTIONAL=D("12.50")
_MIN_HEADROOM=D("1.20")
_TOLERANCE=D("0.00000001")
_QUOTE_STATUS="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
_HISTORY_STATUS="V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING"
_KEY_STATUS="V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
_JOURNAL_STATUS="V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED"


class V40CanaryRiskHalted(RuntimeError):
    """Stable redacted status; no raw venue/SQL body or credentials."""


def _num(raw:Any)->D:
    if type(raw) is bool or type(raw) not in (str,float,int,D):
        raise V40CanaryRiskHalted("V40_CANARY_COLLATERAL_NUMBER_INVALID")
    try:
        value=D(str(raw))
    except (ValueError,InvalidOperation,TypeError):
        raise V40CanaryRiskHalted("V40_CANARY_COLLATERAL_NUMBER_INVALID") from None
    if not value.is_finite():
        raise V40CanaryRiskHalted("V40_CANARY_COLLATERAL_NUMBER_INVALID")
    return value


def _asset_equivalent(
    assets:Any,*,equity:D,available:D,
)->bool:
    """Only a fully cash-backed, flat and unfrozen USDT wallet is admitted."""
    if (
        type(assets) is not list or not assets
        or type(equity) is not D or type(available) is not D
        or equity<=0 or available<0 or available>equity+_TOLERANCE
    ):
        return False
    usdt=[row for row in assets if type(row) is dict
          and row.get("currency")=="USDT"]
    if len(usdt)!=1:
        return False
    try:
        for row in assets:
            if type(row) is not dict or type(row.get("currency")) is not str:
                return False
            if row is usdt[0]:
                continue
            if _num(row.get("equity"))!=0:
                return False
        row=usdt[0]
        for fld in (
            "positionMargin","unrealized","debtAmount","bonus",
            "frozenBalance","contributeMarginAmount",
        ):
            if _num(row.get(fld))!=0:
                return False
        cash=_num(row.get("cashBalance"))
        account_equity=_num(row.get("equity"))
        spendable=_num(row.get("availableBalance"))
        if (
            abs(account_equity-equity)>_TOLERANCE
            or abs(cash-equity)>_TOLERANCE
            or abs(spendable-available)>_TOLERANCE
            or spendable<0
            or spendable+_TOLERANCE<min(_MAX_NOTIONAL,equity*D(".25"))*_MIN_HEADROOM
        ):
            return False
        return True
    except V40CanaryRiskHalted:
        return False


@dataclass(frozen=True)
class V40CanaryRiskReview:
    status:str
    current_risk:V40CurrentRiskEvidence|None=None
    contract_terms:V40ContractTerms|None=None
    api_key_write_verified:bool=False
    signed_pnl_verified:bool=False
    separate_runtime_journal_clean:bool=False
    flat_cross_one_x_bookended:bool=False
    fully_unencumbered_usdt_collateral_verified:bool=False
    safely_below_pilot_daily_loss_cap:bool=False
    future_cross_tier_and_native_stop_execution_certified:bool=False
    database_writes:int=0
    mexc_posts:int=0
    live_authorization:bool=False
    entry_allowed:bool=False


def assess_v40_first_canary_risk(
    *,signal:V40SourceCandidate,
    quote_first:V40CandidateQuote,
    quote_second:V40CandidateQuote,
    assets:Any,
    key:V40KeyPermissionObservation,
    pnl:V40DailyLossEvidence,
    journal:V40RuntimeJournalObservation,
    observed_at:datetime,
)->V40CanaryRiskReview:
    block=lambda code:V40CanaryRiskReview(code)
    if (
        type(signal) is not V40SourceCandidate
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
        or type(quote_first) is not V40CandidateQuote
        or type(quote_second) is not V40CandidateQuote
        or type(key) is not V40KeyPermissionObservation
        or type(pnl) is not V40DailyLossEvidence
        or type(journal) is not V40RuntimeJournalObservation
    ):
        return block("V40_CANARY_RISK_WITNESS_TYPE_INVALID")
    for quote in (quote_first,quote_second):
        if (
            quote.status!=_QUOTE_STATUS
            or quote.symbol!=signal.symbol
            or quote.bookended_flat_account is not True
            or quote.hedge_mode_observed is not True
            or quote.private_risk_row_observed is not True
            or quote.private_short_cross_1x_settings_bookended is not True
            or type(quote.candidate_contract) is not V40ContractTerms
            or quote.candidate_contract.market_state_and_country_verified is not True
            or quote.candidate_contract.private_order_permission_verified is not False
            or quote.entry_allowed is not False
            or quote.live_authorization is not False
            or quote.account_equity_usdt<=0
            or quote.account_available_usdt<0
        ):
            return block("V40_CANARY_RISK_MARKET_FLAT_CROSS_UNVERIFIED")
    # No cached observer timestamp can be promoted to a live risk witness.
    now_ms=int(observed_at.timestamp()*1000)
    if any(
        type(quote.observed_at_ms) is not int
        or not 0<=now_ms-quote.observed_at_ms<=30000
        or quote.book_age_ms<0 or quote.book_age_ms>1500
        for quote in (quote_first,quote_second)
    ):
        return block("V40_CANARY_RISK_MARKET_WITNESS_STALE")
    if (
        quote_first.account_equity_usdt!=quote_second.account_equity_usdt
        or quote_first.account_available_usdt!=quote_second.account_available_usdt
        or quote_first.candidate_contract!=quote_second.candidate_contract
        or quote_first.current_private_short_max_vol!=quote_second.current_private_short_max_vol
        or quote_first.observed_at_ms>quote_second.observed_at_ms
    ):
        return block("V40_CANARY_RISK_MARKET_OR_WALLET_BOOKEND_DRIFT")
    if not _asset_equivalent(
        assets,equity=quote_second.account_equity_usdt,
        available=quote_second.account_available_usdt,
    ):
        return block("V40_CANARY_RISK_UNENCUMBERED_CROSS_WALLET_UNVERIFIED")
    if not (
        key.status==_KEY_STATUS
        and key.api_key_identity_verified is True
        and key.api_key_active_verified is True
        and key.futures_account_read_scope_verified is True
        and key.futures_order_read_scope_verified is True
        and key.futures_order_write_scope_verified is True
        and key.entry_allowed is False and key.live_authorization is False
        and key.mexc_posts==0 and key.database_writes==0
    ):
        return block("V40_CANARY_RISK_CURRENT_PRIVATE_KEY_SCOPE_MISSING")
    if not (
        pnl.status==_HISTORY_STATUS
        and pnl.complete_history_verified is True
        and pnl.complete_two_passes is True
        and pnl.safely_below_12_50 is True
        and pnl.entry_allowed is False and pnl.live_authorization is False
        and pnl.mexc_posts==0 and pnl.database_writes==0
    ):
        return block("V40_CANARY_RISK_SIGNED_DAILY_LOSS_UNVERIFIED")
    try:
        dayloss=_num(pnl.realized_today_loss_usdt)
    except V40CanaryRiskHalted:
        return block("V40_CANARY_RISK_SIGNED_DAILY_LOSS_UNVERIFIED")
    if dayloss<0 or dayloss>=_MAX_NOTIONAL:
        return block("V40_CANARY_RISK_DAILY_LOSS_CAP_REACHED")
    if not (
        journal.status==_JOURNAL_STATUS
        and journal.runtime_role_verified is True
        and journal.repeatable_read_readonly_verified is True
        and journal.v40_tables_observed is True
        and journal.v40_dispatch_state_unresolved_absent is True
        and journal.v40_no_prior_claims_or_slots is True
        and journal.legacy_unresolved_absent is True
        and journal.first_position_journal_clear is True
        and journal.database_writes==0 and journal.mexc_posts==0
        and journal.entry_allowed is False and journal.live_authorization is False
    ):
        return block("V40_CANARY_RISK_DURABLE_MUTATIONS_NOT_CLEAR")
    age=(
        observed_at.astimezone(timezone.utc).timestamp()
        -signal.admission_open_ts
    )
    if not 0<=age<=900:
        return block("V40_CANARY_RISK_STAGE2_NO_LONGER_FRESH")
    # This proof is ONLY for the current completely flat, otherwise
    # unencumbered first-position account at the exact observed point.
    # It cannot certify future cross tiers/venue liquidation/stop execution.
    risk=V40CurrentRiskEvidence(
        current_account_equity_usdt=quote_second.account_equity_usdt,
        available_margin_usdt=min(
            quote_second.account_equity_usdt,
            quote_second.account_available_usdt,
        ),
        portfolio_notional_usdt=D(0),
        realized_today_loss_usdt=dayloss,
        current_owned_positions=0,
        signal_age_seconds=age,
        book_age_ms=quote_second.book_age_ms,
        observed_sell_vwap=D(0),
        inventory_bookended_complete=True,
        no_unowned_positions_or_orders=True,
        cross_collateral_verified=True,
        private_risk_tier_verified=True,
        contract_trading_access_verified=True,
        hedge_mode_verified=True,
        no_unresolved_mutations=True,
    )
    terms=replace(
        quote_second.candidate_contract,
        private_order_permission_verified=True,
    )
    return V40CanaryRiskReview(
        "V40_FIRST_FLAT_CANARY_CURRENT_RISK_OBSERVED_NONAUTHORIZING",
        current_risk=risk,contract_terms=terms,
        api_key_write_verified=True,signed_pnl_verified=True,
        separate_runtime_journal_clean=True,
        flat_cross_one_x_bookended=True,
        fully_unencumbered_usdt_collateral_verified=True,
        safely_below_pilot_daily_loss_cap=True,
    )


class V40FirstCanaryRiskWitness:
    """Coordinator witness backed by independent signed GET and SQL adapters.

    Calls separate signed readers again on every pre- and post-stage inspect;
    never reuses preflight readiness after a candidate or ledger changes.
    """
    def __init__(
        self,*,market:MexcV40CandidateMarketWitness,
        get_permissions:Callable[[],Awaitable[V40KeyPermissionObservation]],
        get_daily_loss:Callable[[],Awaitable[V40DailyLossEvidence]],
        get_runtime_journal:Callable[[],Awaitable[V40RuntimeJournalObservation]],
    )->None:
        if type(market) is not MexcV40CandidateMarketWitness:
            raise V40CanaryRiskHalted("V40_CANARY_MARKET_READER_REQUIRED")
        self.market=market
        self._permissions=get_permissions
        self._loss=get_daily_loss
        self._journal=get_runtime_journal

    async def inspect(
        self,signal:V40SourceCandidate,
    )->tuple[V40CurrentRiskEvidence,V40ContractTerms]:
        try:
            q1=await self.market.observe(signal)
            key=await self._permissions()
            history=await self._loss()
            journal=await self._journal()
            assets=await self.market.private.raw_account_assets()
            q2=await self.market.observe(signal)
            report=assess_v40_first_canary_risk(
                signal=signal,quote_first=q1,quote_second=q2,
                assets=assets,key=key,pnl=history,journal=journal,
                observed_at=datetime.now(timezone.utc),
            )
            if (
                report.status!="V40_FIRST_FLAT_CANARY_CURRENT_RISK_OBSERVED_NONAUTHORIZING"
                or type(report.current_risk) is not V40CurrentRiskEvidence
                or type(report.contract_terms) is not V40ContractTerms
            ):
                raise V40CanaryRiskHalted(report.status)
            return report.current_risk,report.contract_terms
        except V40CanaryRiskHalted:
            raise
        except Exception:
            raise V40CanaryRiskHalted(
                "V40_CANARY_CURRENT_MARKET_DB_OR_HISTORY_READ_FAILED"
            ) from None
