"""Exact, fresh V40 pilot lot and MEXC depth preview for genuine Stage2.

Never a real trade, durable claim, release permit or native stop proof. Read
the restricted scanner again, insist on exactly the monitored Stage2 identity,
then obtain two independently bookended live MEXC market/collateral snapshots
with exact executable lot VWAP. An unverified lot or quote fails closed.

Production connector ONLY has (a) read-only REPEATABLE READ signal SQL,
(b) signed MEXC GET and (c) public MEXC GET. It cannot issue exchange
POST, mutate the DB, enroll a position or self-certify live authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, InvalidOperation
import time
from typing import Any, Mapping, Protocol

from app.t100_live_execution import EntryIntent
from app.t100_live_v40_stage2_source import (
    V40SourceBatch, V40SourceCandidate, PostgresV40Stage2Source,
)
from app.t100_live_v40_candidate_market_witness import (
    V40CandidateQuote, V40ExactDepth, MexcV40CandidateMarketWitness,
)
from app.t100_v40_pilot_exact_lot_review import review_v40_pilot_exact_lot
from app.t100_v40_render_signal_intake import (
    V40SignalIntakeReport, eligible_environment,
)

_FRESH="V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING"
_BOOK="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
_DEPTH="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING"
_VALID_SOURCE="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"


class CandidateBatchReader(Protocol):
    async def latest_candidates(self, *, observed_at:datetime) -> V40SourceBatch: ...


class CandidateMarketReader(Protocol):
    async def observe(self, signal:V40SourceCandidate) -> V40CandidateQuote: ...
    async def quote_exact_intent(
        self, signal:V40SourceCandidate, intent:EntryIntent,
        *, max_slippage_bps:D,
    ) -> tuple[V40CandidateQuote,V40ExactDepth]: ...


@dataclass(frozen=True)
class V40ExactLotPreview:
    status: str
    signal_id: int | None = None
    symbol: str | None = None
    tier: str | None = None
    capped_target_usdt: str | None = None
    executable_notional_usdt: str | None = None
    executable_contracts: str | None = None
    modeled_stop_loss_usdt: str | None = None
    displayed_sell_vwap: str | None = None
    order_book_age_ms: int | None = None
    exact_lot_depth_verified: bool = False
    native_market_stop_verified: bool = False
    cross_collateral_certified: bool = False
    exchange_order_permission_certified: bool = False
    read_only: bool = True
    mexc_posts: int = 0
    database_writes: int = 0
    entry_allowed: bool = False
    live_authorization: bool = False


def _d(raw:Any) -> D | None:
    if raw is None or type(raw) is bool:
        return None
    try:
        value=D(str(raw))
    except (TypeError,ValueError,InvalidOperation):
        return None
    return value if value.is_finite() and value>0 else None


def _base(status:str, monitor:V40SignalIntakeReport | None) -> V40ExactLotPreview:
    return V40ExactLotPreview(
        status=status,
        signal_id=monitor.signal_id if type(monitor) is V40SignalIntakeReport else None,
        symbol=monitor.symbol if type(monitor) is V40SignalIntakeReport else None,
        tier=monitor.tier if type(monitor) is V40SignalIntakeReport else None,
    )


async def preview_exact_genuine_signal(
    *, monitor:V40SignalIntakeReport,
    source:CandidateBatchReader,
    market:CandidateMarketReader,
    observed_at:datetime,
    clock_ms:Any,
) -> V40ExactLotPreview:
    """Bounded one-shot safe preview. No retries and no mutation privileges."""
    fail=lambda reason:_base(reason,monitor)
    if (
        type(monitor) is not V40SignalIntakeReport
        or monitor.status!=_FRESH
        or monitor.signal_count!=1
        or type(monitor.signal_id) is not int or monitor.signal_id<=0
        or type(monitor.symbol) is not str
        or not monitor.symbol.endswith("_USDT")
        or monitor.symbol in ("BTC_USDT","ETH_USDT")
        or monitor.tier not in ("LOW_100","HIGH_225")
        or monitor.fresh_source_verified is not True
        or monitor.current_flat_account_verified is not True
        or monitor.entry_allowed is not False
        or monitor.live_authorization is not False
        or monitor.mexc_posts!=0 or monitor.database_writes!=0
        or _d(monitor.theoretical_v40_notional_usdt) is None
        or _d(monitor.account_equity_usdt) is None
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or not callable(clock_ms)
    ):
        return fail("V40_EXACT_PREVIEW_MONITOR_CANDIDATE_INVALID")
    try:
        tape=await source.latest_candidates(observed_at=observed_at)
    except Exception:
        return fail("V40_EXACT_PREVIEW_RESTRICTED_SOURCE_UNAVAILABLE")
    if (
        type(tape) is not V40SourceBatch
        or tape.status!=_VALID_SOURCE
        or tape.read_only_repeatable_read is not True
        or tape.history_complete is not True
        or tape.entry_allowed is not False
        or tape.live_authorization is not False
        or type(tape.candidates) is not tuple
        or len(tape.candidates)!=1
    ):
        return fail("V40_EXACT_PREVIEW_SOURCE_NOT_UNIQUE_OR_VERIFIED")
    signal=tape.candidates[0]
    if (
        type(signal) is not V40SourceCandidate
        or signal.signal_id!=monitor.signal_id
        or signal.symbol!=monitor.symbol
        or signal.tier!=monitor.tier
        or signal.entry_allowed is not False
        or signal.live_authorization is not False
        or not 0<=signal.observed_age_seconds<=900
    ):
        return fail("V40_EXACT_PREVIEW_MONITORED_SIGNAL_CHANGED")
    try:
        first=await market.observe(signal)
        if (
            type(first) is not V40CandidateQuote
            or first.status!=_BOOK
            or first.symbol!=signal.symbol
            or first.bookended_flat_account is not True
            or first.hedge_mode_observed is not True
            or first.private_risk_row_observed is not True
            or first.private_short_cross_1x_settings_bookended is not True
            or first.full_cross_collateral_verified is not False
            or first.private_key_order_permission_verified is not False
            or first.entry_allowed is not False
            or first.live_authorization is not False
            or first.candidate_contract.private_order_permission_verified is not False
            or first.candidate_contract.market_state_and_country_verified is not True
            or abs(first.account_equity_usdt-_d(monitor.account_equity_usdt))>D("0.00000001")
        ):
            return fail("V40_EXACT_PREVIEW_ACCOUNT_OR_CROSS_CONFIGURATION_DRIFT")
        contract=first.candidate_contract
        lot=review_v40_pilot_exact_lot(
            signal_id=signal.signal_id,symbol=signal.symbol,
            reference_price=str(signal.entry_reference_price),
            frozen_modeled_notional_usdt=monitor.theoretical_v40_notional_usdt,
            equity_usdt=str(first.account_equity_usdt),
            available_usdt=str(first.account_available_usdt),
            contract_size=str(contract.contract_size),
            volume_step=str(contract.volume_step),
            min_volume=str(contract.min_volume),
            max_volume=str(contract.max_volume),
            price_tick=str(contract.price_tick),
            max_order_usdt="12.50",max_equity_fraction="0.25",
            max_daily_loss_usdt="12.50",
            realized_today_loss_usdt="0",max_slippage_bps="100",
        )
        if lot.status!="V40_PILOT_LOT_FEASIBLE_NONAUTHORIZING":
            return fail("V40_EXACT_PREVIEW_LOT_OR_MODELED_STOP_LIMIT_REJECTED")
        if lot.executable_contracts>first.current_private_short_max_vol:
            return fail("V40_EXACT_PREVIEW_PRIVATE_SHORT_TIER_EXCEEDED")
        intent=EntryIntent.build(
            signal_id=signal.signal_id,symbol=signal.symbol,
            reference_price=D(str(signal.entry_reference_price)),
            notional_usdt=lot.capped_target_usdt,
            contract_size=contract.contract_size,
            volume_step=contract.volume_step,
            min_volume=contract.min_volume,
            max_volume=contract.max_volume,
            price_tick=contract.price_tick,
        )
        if (
            intent.notional_usdt!=lot.executable_notional_usdt
            or intent.volume_contracts!=lot.executable_contracts
        ):
            return fail("V40_EXACT_PREVIEW_GRID_INTENT_MISMATCH")
        second,depth=await market.quote_exact_intent(
            signal,intent,max_slippage_bps=D("100"),
        )
        now=clock_ms()
        if (
            type(second) is not V40CandidateQuote or type(depth) is not V40ExactDepth
            or second.status!=_BOOK
            or second.symbol!=signal.symbol
            or second.candidate_contract!=contract
            or second.bookended_flat_account is not True
            or second.hedge_mode_observed is not True
            or second.private_short_cross_1x_settings_bookended is not True
            or second.private_risk_row_observed is not True
            or second.live_authorization is not False
            or second.entry_allowed is not False
            or second.candidate_contract.private_order_permission_verified is not False
            or abs(second.account_equity_usdt-first.account_equity_usdt)>D("0.00000001")
            or second.account_available_usdt<lot.capped_target_usdt
            or second.current_private_short_max_vol<intent.volume_contracts
            or depth.status!=_DEPTH
            or depth.symbol!=signal.symbol
            or depth.signal_id!=signal.signal_id
            or depth.contracts!=intent.volume_contracts
            or depth.order_fits_depth is not True
            or depth.adverse_slippage_bps>D("100")
            or depth.entry_allowed is not False
            or depth.live_authorization is not False
            or type(now) is not int
            or type(second.observed_at_ms) is not int
            or not 0<=now-second.observed_at_ms<=1500
            or type(depth.age_ms) is not int
            or not 0<=depth.age_ms<=1500
            or depth.sell_vwap<=0
        ):
            return fail("V40_EXACT_PREVIEW_CURRENT_DEPTH_OR_COLLATERAL_UNVERIFIED")
        return V40ExactLotPreview(
            status="V40_PILOT_REAL_SIGNAL_EXACT_DEPTH_QUOTED_NONAUTHORIZING",
            signal_id=signal.signal_id,symbol=signal.symbol,tier=signal.tier,
            capped_target_usdt=str(lot.capped_target_usdt),
            executable_notional_usdt=str(intent.notional_usdt),
            executable_contracts=str(intent.volume_contracts),
            modeled_stop_loss_usdt=str(lot.modeled_stop_loss_usdt),
            displayed_sell_vwap=str(depth.sell_vwap),
            order_book_age_ms=depth.age_ms,
            exact_lot_depth_verified=True,
        )
    except Exception:
        return fail("V40_EXACT_PREVIEW_MARKET_GET_OR_LOT_INVALID")


async def observe_render_exact_lot_preview(
    env:Mapping[str,str], monitor:V40SignalIntakeReport,
) -> V40ExactLotPreview:
    """Production strict GET/SELECT-only adapter, no order credentials reuse."""
    if not (
        eligible_environment(env)
        and env.get("T100_V40_REAL_SIGNAL_MONITOR_ENABLED")=="true"
        and env.get("T100_V40_MONITOR_SINGLE_POSITION_SCOPE")=="true"
        and type(monitor) is V40SignalIntakeReport
        and monitor.status==_FRESH
    ):
        return _base("V40_EXACT_PREVIEW_PAPER_KILL_OR_SCOPE_HALT",monitor)
    import asyncpg
    import httpx
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    from scripts.t100_v40_pilot_get_only import KeychainSignedGetOnly
    key,secret=env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return _base("V40_EXACT_PREVIEW_ACCOUNT_KEYS_UNAVAILABLE",monitor)
    pool=None
    signed=None
    try:
        pool=await asyncpg.create_pool(
            dsn=env["T100_V40_SIGNAL_READER_DSN"],
            ssl="require",min_size=1,max_size=1,timeout=9,
            command_timeout=12,
            server_settings={"default_transaction_read_only":"on"},
        )
        signed=KeychainSignedGetOnly(key=key,secret=secret)
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",trust_env=False,
            follow_redirects=False,timeout=8,
        ) as public:
            market=MexcV40CandidateMarketWitness(
                private=ReadOnlyMexcProbe(signed),public=public,
                clock_ms=lambda:int(time.time()*1000),
                max_book_age_ms=1500,max_cycle_ms=5000,
            )
            return await preview_exact_genuine_signal(
                monitor=monitor,
                source=PostgresV40Stage2Source(
                    pool,restricted_source_reader=True,
                ),
                market=market,observed_at=datetime.now(timezone.utc),
                clock_ms=lambda:int(time.time()*1000),
            )
    except Exception:
        return _base("V40_EXACT_PREVIEW_RESTRICTED_CONNECTION_OR_GET_FAILED",monitor)
    finally:
        if signed is not None:
            await signed.close()
        if pool is not None:
            await pool.close()
