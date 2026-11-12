"""Continuously observe the REAL frozen V40 scanner and isolated MEXC pilot.

Strict first-stage activation of the genuine strategy feed; **NOT** a live
execution adapter. Always requires the existing paper/kill-latched V40
worker. Does not generate fake BTC signals, POST orders, change stops or
touch scanner/BOXWAVE. Only a *fresh* validated scanner signal can appear.

Every cycle:
  1. Fresh production PostgreSQL Stage2 read using only the independently
     audited column-level read-only t100_v40_signal_reader login.
  2. Frozen V40 VOL30 and causal 49-bar economics from the actual signal.
  3. TWO signed account-wide MEXC GET bookends even when source is empty;
     refuse unexpected positions, stop orders, pending orders or nonflat
     equity before proceeding to a later cycle.
  4. Structured bounded logs. Candidate remains NONAUTHORIZING: the native
     stop/trailing/fill/recovery runtime is NOT connected or certified.

Source and account failure halt the process; Render's normal restart
behavior never upgrades authorizations. No database writes, no POST.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any, Awaitable, Callable, Mapping

from app.t100_v40_render_signal_intake import (
    V40SignalIntakeReport, _once, eligible_environment,
)
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult, observe_live_pilot
from scripts.t100_v40_pilot_capability_get_only import (
    PilotCapabilityReport, observe_pilot_capability,
)
from app.t100_v40_render_journal_readonly import (
    DSN_KEY, V40RuntimeJournalObservation, observe_render_runtime_journal,
)
from app.t100_v40_render_exact_lot_preview import (
    V40ExactLotPreview, observe_render_exact_lot_preview,
)

ACTIVATION_FLAG="T100_V40_REAL_SIGNAL_MONITOR_ENABLED"
POLL_SECONDS=60
# Candidate-only extra source + two full MEXC depth/account bookends
# are bounded independently. No candidate = original 60-second cadence.
MAX_CYCLE_SECONDS=220
EXACT_LOT_FLAG="T100_V40_EXACT_LOT_GET_ONLY_ENABLED"


@dataclass(frozen=True)
class V40SupervisedPilotObservation:
    status: str
    source_status: str | None = None
    signal_id: int | None = None
    signal_symbol: str | None = None
    signal_tier: str | None = None
    pre24_vol_pct: float | None = None
    theoretical_notional_usdt: str | None = None
    fresh_signal_count: int = 0
    account_equity_usdt: str | None = None
    account_flat_verified: bool = False
    source_verified: bool = False
    real_signal_economics_observed: bool = False
    candidate_public_contract_and_cross_1x_verified: bool = False
    candidate_exact_strategy_lot_verified: bool = False
    candidate_exact_lot_status: str | None = None
    candidate_executable_notional_usdt: str | None = None
    candidate_executable_contracts: str | None = None
    candidate_exact_lot_vwap: str | None = None
    candidate_exact_lot_book_age_ms: int | None = None
    candidate_capability_status: str | None = None
    runtime_journal_status: str | None = None
    first_position_journal_clean: bool = False
    enabled_position_capacity: int = 0
    live_adapter_connected: bool = False
    native_stop_management_connected: bool = False
    continuous_nonflat_guardian_connected: bool = False
    mexc_orders_sent: int = 0
    database_writes: int = 0
    live_authorization: bool = False
    entry_allowed: bool = False


def monitor_environment(env: Mapping[str,str]) -> bool:
    return (
        eligible_environment(env)
        and env.get(ACTIVATION_FLAG)=="true"
        and env.get("T100_V40_MONITOR_SINGLE_POSITION_SCOPE")=="true"
    )


def _positive_usdt(value:Any)->bool:
    try:
        n=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        return False
    return n.is_finite() and n>0


async def supervise_real_v40_cycle(
    env: Mapping[str,str], *,
    signal_read: Callable[[Mapping[str,str]], Awaitable[V40SignalIntakeReport]]=_once,
    account_observe: Callable[...,Awaitable[PilotGetOnlyResult]]=observe_live_pilot,
    capability_observe: Callable[...,Awaitable[PilotCapabilityReport]]=observe_pilot_capability,
    journal_observe: Callable[[Mapping[str,str]],Awaitable[V40RuntimeJournalObservation]]=observe_render_runtime_journal,
    exact_lot_observe: Callable[[Mapping[str,str],V40SignalIntakeReport],Awaitable[V40ExactLotPreview]]=observe_render_exact_lot_preview,
) -> V40SupervisedPilotObservation:
    def blocked(status:str, source:V40SignalIntakeReport|None=None):
        return V40SupervisedPilotObservation(
            status=status,
            source_status=source.status if source else None,
            signal_id=source.signal_id if source else None,
            signal_symbol=source.symbol if source else None,
            signal_tier=source.tier if source else None,
            pre24_vol_pct=source.pre24_vol_pct if source else None,
            theoretical_notional_usdt=(
                source.theoretical_v40_notional_usdt if source else None
            ),
            fresh_signal_count=source.signal_count if source else 0,
            source_verified=source.fresh_source_verified if source else False,
        )
    if not monitor_environment(env):
        return blocked("V40_REAL_SIGNAL_MONITOR_PAPER_KILL_OR_SCOPE_REQUIRED")
    try:
        source=await asyncio.wait_for(signal_read(env),timeout=27)
    except Exception:
        return blocked("V40_REAL_SIGNAL_SOURCE_OR_TIMEOUT_HALT")
    if (
        type(source) is not V40SignalIntakeReport
        or source.fresh_source_verified is not True
        or source.mexc_posts!=0 or source.database_writes!=0
        or source.live_authorization is not False
        or source.entry_allowed is not False
    ):
        return blocked("V40_REAL_SIGNAL_SOURCE_UNVERIFIED_HALT")
    allowed_status=(
        "V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED",
        "V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
        "V40_SIGNAL_INTAKE_VOL30_SKIP_REAL_SOURCE",
    )
    if source.status not in allowed_status:
        return blocked("V40_REAL_SIGNAL_SOURCE_OR_CANDIDATE_HALT",source)
    if source.status==allowed_status[1] and (
        type(source.signal_id) is not int or source.signal_id<=0
        or type(source.symbol) is not str or not source.symbol.endswith("_USDT")
        or source.signal_count!=1
        or not _positive_usdt(source.theoretical_v40_notional_usdt)
        or source.current_flat_account_verified is not True
    ):
        return blocked("V40_REAL_SIGNAL_CANDIDATE_ECONOMICS_HALT",source)
    if source.status==allowed_status[0] and (
        source.signal_count!=0 or source.signal_id is not None
        or source.symbol is not None
    ):
        return blocked("V40_REAL_SIGNAL_EMPTY_SOURCE_SHAPE_HALT",source)
    # VOL30_SKIP is a NORMAL strategy rejection, not a daemon crash.
    # Require independently source-verified identity and >=30% volatility.
    # Still perform both journal and MEXC flat-account GET bookends before
    # reporting a healthy skip; no candidate-specific capability GET or POST.
    if source.status==allowed_status[2] and (
        type(source.signal_id) is not int or source.signal_id<=0
        or type(source.symbol) is not str or not source.symbol.endswith("_USDT")
        or source.symbol in ("BTC_USDT","ETH_USDT")
        or source.tier not in ("LOW_100","HIGH_225")
        or type(source.signal_count) is not int or source.signal_count!=1
        or type(source.pre24_vol_pct) not in (int,float)
        or not 30 <= source.pre24_vol_pct < float("inf")
        or source.current_flat_account_verified is not False
        or source.theoretical_v40_notional_usdt is not None
    ):
        return blocked("V40_REAL_SIGNAL_VOL30_SKIP_SHAPE_HALT",source)

    # Inspect actual restricted live journal if separately configured.
    # Do not treat a missing journal DSN as permission to trade. The flat
    # account monitor may still run without a candidate, but a candidate
    # cannot advance without a clean first-position ledger snapshot.
    journal_clean=False
    journal_status=None
    if env.get(DSN_KEY):
        try:
            journal=await asyncio.wait_for(journal_observe(env),timeout=12)
        except Exception:
            return blocked("V40_REAL_SIGNAL_RUNTIME_JOURNAL_READ_FAILED_HALT",source)
        if (
            type(journal) is not V40RuntimeJournalObservation
            or journal.status!="V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED"
            or journal.first_position_journal_clear is not True
            or journal.runtime_role_verified is not True
            or journal.repeatable_read_readonly_verified is not True
            or journal.v40_no_prior_claims_or_slots is not True
            or journal.legacy_unresolved_absent is not True
            or journal.database_writes!=0 or journal.mexc_posts!=0
            or journal.entry_allowed is not False
            or journal.live_authorization is not False
        ):
            return blocked("V40_REAL_SIGNAL_RUNTIME_JOURNAL_UNRESOLVED_HALT",source)
        journal_clean=True
        journal_status=journal.status
    elif source.signal_id is not None:
        return blocked("V40_REAL_SIGNAL_RUNTIME_JOURNAL_DSN_REQUIRED_HALT",source)

    key,secret=env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12
        or type(secret) is not str or len(secret)<16
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_KEYS_MISSING",source)
    try:
        account=await asyncio.wait_for(
            account_observe(key=key,secret=secret),timeout=23,
        )
    except Exception:
        # Open positions, unsupported margin, missing stops and HTTP errors
        # are all safety halts. Never assume that an empty source means flat.
        return blocked("V40_REAL_SIGNAL_ACCOUNT_NOT_FLAT_OR_GET_FAILED_HALT",source)
    if not (
        type(account) is PilotGetOnlyResult
        and account.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
        and account.samples==2
        and account.position_count==0
        and account.native_stop_count==0
        and account.inventory_bookends_equal is True
        and account.hedge_mode_observed is True
        and account.mexc_posts==0 and account.db_mutations==0
        and account.live_authorization is False
        and _positive_usdt(account.account_equity_usdt)
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_INVENTORY_UNVERIFIED_HALT",source)
    if source.status==allowed_status[1] and (
        source.account_equity_usdt is None
        or abs(Decimal(source.account_equity_usdt) -
               Decimal(str(account.account_equity_usdt)))>Decimal("0.00000001")
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_EQUITY_DRIFT_HALT",source)
    # A real candidate triggers additional PUBLIC futures-contract and
    # separately signed PRIVATE SHORT CROSS 1x risk/leverage GET witnesses.
    # This does NOT establish the executable lot for the capped order,
    # server-side POST permission, stop attachment, or future risk tiers.
    capability_verified=False
    capability_status=None
    if source.status==allowed_status[1]:
        try:
            cap=await asyncio.wait_for(capability_observe(
                key=key,secret=secret,symbol=source.symbol,
            ),timeout=65)
        except Exception:
            return blocked("V40_REAL_SIGNAL_PRIVATE_CROSS_CAPABILITY_GET_FAILED_HALT",source)
        if (
            type(cap) is not PilotCapabilityReport
            or cap.symbol!=source.symbol
            or cap.status!="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING"
            or cap.flat_hedge_account_bookended is not True
            or cap.public_futures_contract_eligible_observed is not True
            or cap.current_private_short_cross_1x_tier_observed is not True
            or cap.one_contract_on_public_lot_grid is not True
            or cap.current_short_tier_supports_one_contract is not True
            or cap.short_margin_mode_observed!="CROSS"
            or cap.short_leverage_observed!=1
            or cap.mexc_posts!=0 or cap.db_mutations!=0
            or cap.entry_allowed is not False
            or cap.live_authorization is not False
        ):
            return blocked("V40_REAL_SIGNAL_PRIVATE_CROSS_CAPABILITY_UNVERIFIED_HALT",source)
        capability_verified=True
        capability_status=cap.status

    # Optional real-candidate-only SECOND restricted Stage2 read, exact
    # exchange-lot sizing, and full-quantity fresh order-book depth quote.
    # A declined/failed preview is a NONAUTHORIZING observation, NOT a
    # worker-crash reason. Neither a source skip nor an empty feed makes
    # public/private candidate GETs. This can never place an order.
    exact_preview=None
    exact_preview_verified=False
    exact_preview_status=None
    if (
        source.status==allowed_status[1]
        and env.get(EXACT_LOT_FLAG)=="true"
    ):
        try:
            exact_preview=await asyncio.wait_for(
                exact_lot_observe(env,source),timeout=65,
            )
        except Exception:
            exact_preview_status="V40_EXACT_PREVIEW_GET_TIMEOUT_OR_FAILURE"
        else:
            if (
                type(exact_preview) is V40ExactLotPreview
                and exact_preview.signal_id==source.signal_id
                and exact_preview.symbol==source.symbol
                and exact_preview.tier==source.tier
                and exact_preview.mexc_posts==0
                and exact_preview.database_writes==0
                and exact_preview.entry_allowed is False
                and exact_preview.live_authorization is False
                and exact_preview.read_only is True
            ):
                exact_preview_status=exact_preview.status
                exact_preview_verified=(
                    exact_preview.status=="V40_PILOT_REAL_SIGNAL_EXACT_DEPTH_QUOTED_NONAUTHORIZING"
                    and exact_preview.exact_lot_depth_verified is True
                    and exact_preview.native_market_stop_verified is False
                    and exact_preview.cross_collateral_certified is False
                    and exact_preview.exchange_order_permission_certified is False
                )
            else:
                exact_preview_status="V40_EXACT_PREVIEW_RESULT_SHAPE_UNVERIFIED"

    return V40SupervisedPilotObservation(
        status=(
            "V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED"
            if source.status==allowed_status[1] else
            "V40_REAL_VOL30_SIGNAL_SKIPPED_ACCOUNT_FLAT"
            if source.status==allowed_status[2] else
            "V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT"
        ),
        source_status=source.status,
        signal_id=source.signal_id,
        signal_symbol=source.symbol,
        signal_tier=source.tier,
        pre24_vol_pct=source.pre24_vol_pct,
        theoretical_notional_usdt=source.theoretical_v40_notional_usdt,
        fresh_signal_count=source.signal_count,
        account_equity_usdt=str(account.account_equity_usdt),
        account_flat_verified=True,
        source_verified=True,
        real_signal_economics_observed=source.status==allowed_status[1],
        candidate_public_contract_and_cross_1x_verified=capability_verified,
        candidate_capability_status=capability_status,
        candidate_exact_lot_status=exact_preview_status,
        candidate_executable_notional_usdt=(
            exact_preview.executable_notional_usdt
            if exact_preview_verified else None
        ),
        candidate_executable_contracts=(
            exact_preview.executable_contracts
            if exact_preview_verified else None
        ),
        candidate_exact_lot_vwap=(
            exact_preview.displayed_sell_vwap
            if exact_preview_verified else None
        ),
        candidate_exact_lot_book_age_ms=(
            exact_preview.order_book_age_ms
            if exact_preview_verified else None
        ),
        runtime_journal_status=journal_status,
        first_position_journal_clean=journal_clean,
        # Recheck actual integer lot and intended notional independently
        # against live depth before any entry can ever be authorized.
        candidate_exact_strategy_lot_verified=exact_preview_verified,
    )


async def run_supervised_signal_monitor(
    env:Mapping[str,str], *, max_cycles:int|None=None,
    observe_cycle:Callable[...,Awaitable[V40SupervisedPilotObservation]]=supervise_real_v40_cycle,
    delay: Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->int:
    if not monitor_environment(env):
        print(json.dumps(asdict(V40SupervisedPilotObservation(
            "V40_REAL_SIGNAL_MONITOR_PAPER_KILL_OR_SCOPE_REQUIRED"
        )),sort_keys=True),flush=True)
        return 2
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles<1):
        return 2
    last_signal=None
    number=0
    while max_cycles is None or number<max_cycles:
        try:
            result=await asyncio.wait_for(
                observe_cycle(env),timeout=MAX_CYCLE_SECONDS,
            )
        except Exception:
            result=V40SupervisedPilotObservation("V40_REAL_SIGNAL_MONITOR_CYCLE_TIMEOUT_OR_ERROR_HALT")
        number+=1
        ok=(
            type(result) is V40SupervisedPilotObservation
            and result.status in (
                "V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT",
                "V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED",
                "V40_REAL_VOL30_SIGNAL_SKIPPED_ACCOUNT_FLAT",
            )
            and result.source_verified is True
            and result.account_flat_verified is True
            and result.mexc_orders_sent==0 and result.database_writes==0
            and result.live_authorization is False and result.entry_allowed is False
        )
        # Explicit heartbeat every 5 cycles and every genuinely new signal.
        if not ok or number==1 or number%5==0 or (
            result.signal_id is not None and result.signal_id!=last_signal
        ):
            print(json.dumps(asdict(result),sort_keys=True),flush=True)
        if not ok:
            return 2
        last_signal=result.signal_id if result.signal_id is not None else last_signal
        if max_cycles is None or number<max_cycles:
            await delay(POLL_SECONDS)
    return 0


def main()->int:
    try:
        return asyncio.run(run_supervised_signal_monitor(dict(os.environ)))
    except (KeyboardInterrupt,SystemExit):
        return 0


if __name__=="__main__":
    raise SystemExit(main())
