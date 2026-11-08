"""One-shot V40 dispatch boundary: frozen strategy, durable claim, ONE POST.

Not wired into a running service. LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED blocks
the certified transport permit in the shipped configuration. This module
never schedules signals, edits stops, alters environment, or enables trading.

A future separately reviewed coordinator must supply fresh, independently
verified strategy/risk/account observations and a durably staged intent.
Claim is irreversible: after claim, any exception/unknown response is
RECONCILIATION ONLY. Absence on an order GET is NEVER permission to retry.

The 75%-above-entry short stop is the frozen V40 strategy, NOT the tight
5-bp BTC pilot. CROSS 1x can still lose more than modeled because of gaps,
fees, collateral, liquidation, exchange downtime and missing protection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal as D, ROUND_UP
from typing import Protocol, Any

from app.t100_live_authorization import (
    LiveMutationPermit, require_certified_transport_permit,
)
from app.t100_live_execution import (
    EntryIntent, LiveCaps, modeled_short_loss_at_native_stop,
)
from app.t100_live_four_slot_planner import FourSlotPlan
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_dispatch_claims import FourSlotDispatchClaim
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_strategy_parity_gate import assess_strategy_parity_report
from app.t100_v40_admission import V40Admission


class V40DispatchHalted(RuntimeError):
    """Only safe redacted outcome codes; never a venue/DB error body."""


@dataclass(frozen=True)
class V40CurrentRiskEvidence:
    """Inputs from independently protected account/market witnesses.

    These assertions are not proved by constructing this record. The
    independent production witness collector remains a release blocker.
    """
    current_account_equity_usdt: D
    available_margin_usdt: D
    portfolio_notional_usdt: D
    realized_today_loss_usdt: D
    current_owned_positions: int
    signal_age_seconds: float
    book_age_ms: int
    observed_sell_vwap: D
    inventory_bookended_complete: bool = False
    no_unowned_positions_or_orders: bool = False
    cross_collateral_verified: bool = False
    private_risk_tier_verified: bool = False
    contract_trading_access_verified: bool = False
    hedge_mode_verified: bool = False
    no_unresolved_mutations: bool = False


@dataclass(frozen=True)
class V40DispatchResult:
    status: str
    durable_claim_created: bool
    post_attempted: bool
    must_reconcile_external_oid: bool
    entry_allowed: bool = False
    live_authorization: bool = False
    additional_post_allowed: bool = False


class DispatchClaimWriter(Protocol):
    async def claim(
        self, *, stage: DurableAttemptStage, lease: LeaderLease,
    ) -> FourSlotDispatchClaim: ...


class UnknownAttemptJournal(Protocol):
    async def latch_unknown(
        self, *, signal_id: int, external_oid: str, lease: LeaderLease,
    ) -> None: ...


class ShortEntryTransport(Protocol):
    open_type: int
    async def submit_short_with_stop(
        self, intent: EntryIntent, *, permit: LiveMutationPermit,
    ) -> str: ...


def _num(value: Any, *, nonnegative: bool = False) -> bool:
    return (
        type(value) is D and value.is_finite()
        and (value >= 0 if nonnegative else value > 0)
    )


def _preclaim_validate(
    *, plan: FourSlotPlan, intent: EntryIntent,
    stage: DurableAttemptStage, lease: LeaderLease,
    risk: V40CurrentRiskEvidence, caps: LiveCaps,
    permit: LiveMutationPermit, price_tick: D,
    transport: ShortEntryTransport,
    parity_report: dict[str, Any],
) -> None:
    # Actual permit issuer and adapter certification are separate. This
    # MUST be the first decision; never trust operator limits alone.
    require_certified_transport_permit(permit)
    if (
        type(plan) is not FourSlotPlan
        or plan.action != "ADMIT"
        or plan.entry_allowed is not False
        or plan.live_authorization is not False
        or type(plan.v40_admission) is not V40Admission
        or plan.v40_admission.action != "ADMIT"
        or plan.candidate_symbol != intent.symbol
        or type(stage) is not DurableAttemptStage
        or type(intent) is not EntryIntent
        or type(lease) is not LeaderLease
        or type(risk) is not V40CurrentRiskEvidence
        or type(caps) is not LiveCaps
        or stage.state != "SUBMITTING"
        or stage.post_attempted is not False
        or stage.reconciliation_required is not True
        or stage.entry_allowed is not False
        or stage.live_authorization is not False
        or stage.signal_id != intent.signal_id
        or stage.external_oid != intent.external_oid
        or lease.lease_name != "t100-live-executor"
        or type(lease.fencing_token) is not int
        or lease.fencing_token <= 0
        or lease.live_authorization is not False
        or type(getattr(transport, "open_type", None)) is not int
        or transport.open_type != 2
        or caps.margin_type != 2
        or caps.leverage != 1
        or permit.leverage != 1
    ):
        raise V40DispatchHalted("V40_DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID")

    # Historic entry parity alone is NOT enough: full V40 chronological
    # strategy parity is a distinct release gate. Live equivalence still
    # requires independent testing beyond this report.
    parity = assess_strategy_parity_report(parity_report)
    if (
        parity.get("strategy_parity_verified") is not True
        or parity.get("live_authorization") is not False
    ):
        raise V40DispatchHalted("V40_FULL_STRATEGY_PARITY_NOT_VERIFIED")

    if not (
        type(intent.signal_id) is int and intent.signal_id > 0
        and re.fullmatch(r"[A-Z0-9]+_USDT", intent.symbol)
        and intent.symbol not in ("BTC_USDT", "ETH_USDT")
        and re.fullmatch(r"t100v1e[a-f0-9]{20}", intent.external_oid)
        and _num(price_tick)
        and all(_num(v) for v in (
            intent.reference_price, intent.notional_usdt,
            intent.volume_contracts, intent.stop_price,
        ))
        and intent.stop_price == (
            intent.reference_price * D("1.75") / price_tick
        ).to_integral_value(rounding=ROUND_UP) * price_tick
        and intent.notional_usdt <= D(str(plan.modeled_notional_usdt))
        and intent.notional_usdt <= D(str(plan.v40_admission.modeled_notional_usdt))
    ):
        raise V40DispatchHalted("V40_FROZEN_STOP_OR_MODELED_NOTIONAL_MISMATCH")

    if (
        any(type(getattr(risk, flag)) is not bool
            or getattr(risk, flag) is not True for flag in (
                "inventory_bookended_complete",
                "no_unowned_positions_or_orders",
                "cross_collateral_verified",
                "private_risk_tier_verified",
                "contract_trading_access_verified",
                "hedge_mode_verified",
                "no_unresolved_mutations",
            ))
        or type(risk.current_owned_positions) is not int
        or not 0 <= risk.current_owned_positions <= 3
        or risk.current_owned_positions != plan.occupied_slots
        or not all(_num(x, nonnegative=True) for x in (
            risk.current_account_equity_usdt,
            risk.available_margin_usdt,
            risk.portfolio_notional_usdt,
            risk.realized_today_loss_usdt,
        ))
        or risk.current_account_equity_usdt <= 0
        or not _num(risk.observed_sell_vwap)
        or type(risk.book_age_ms) is not int
        or not 0 <= risk.book_age_ms <= caps.max_book_age_ms
        or type(risk.signal_age_seconds) not in (float, int)
        or not 0 <= risk.signal_age_seconds <= 900
    ):
        raise V40DispatchHalted("V40_ACCOUNT_RISK_OR_FRESHNESS_NOT_CERTIFIED")

    if (
        intent.notional_usdt > min(
            caps.max_order_usdt, permit.max_position_notional_usdt,
        )
        or risk.portfolio_notional_usdt + intent.notional_usdt > min(
            caps.max_portfolio_usdt, permit.max_total_notional_usdt,
        )
        or risk.realized_today_loss_usdt >= min(
            caps.max_daily_loss_usdt, permit.max_daily_loss_usdt,
        )
        or intent.notional_usdt > risk.available_margin_usdt
    ):
        raise V40DispatchHalted("V40_OPERATOR_RISK_CAP_OR_MARGIN_EXCEEDED")

    from app.t100_live_execution import adverse_bps
    if adverse_bps(
        risk.observed_sell_vwap, intent.reference_price, "sell",
    ) > caps.max_slippage_bps:
        raise V40DispatchHalted("V40_ENTRY_SLIPPAGE_CAP_EXCEEDED")
    modeled_loss = modeled_short_loss_at_native_stop(
        intent, max_adverse_slippage_bps=caps.max_slippage_bps,
    )
    if risk.realized_today_loss_usdt + modeled_loss >= min(
        caps.max_daily_loss_usdt, permit.max_daily_loss_usdt,
    ):
        raise V40DispatchHalted("V40_MODELED_STOP_EXCEEDS_DAILY_LOSS_CAP")


async def dispatch_one_staged_v40(
    *, plan: FourSlotPlan, intent: EntryIntent,
    stage: DurableAttemptStage, lease: LeaderLease,
    risk: V40CurrentRiskEvidence, caps: LiveCaps,
    permit: LiveMutationPermit, price_tick: D,
    parity_report: dict[str, Any],
    claims: DispatchClaimWriter,
    journal: UnknownAttemptJournal,
    transport: ShortEntryTransport,
) -> V40DispatchResult:
    """At most ONE POST, after an irreversible DB claim. No built-in retry.

    This function does not create strategy signals, connect a production
    worker, declare venue readiness, or submit unless independently
    certified permit and all pre-claim checks succeed. Do NOT deploy
    the function without reviewed collection of all risk witnesses.
    """
    _preclaim_validate(
        plan=plan, intent=intent, stage=stage, lease=lease, risk=risk,
        caps=caps, permit=permit, price_tick=price_tick,
        transport=transport, parity_report=parity_report,
    )
    try:
        claim = await claims.claim(stage=stage, lease=lease)
    except Exception:
        # The irreversible claim might have been committed before a DB
        # timeout. Do NOT assume absence; reconcile before ANY new action.
        raise V40DispatchHalted("V40_DISPATCH_CLAIM_UNKNOWN_NO_RETRY") from None
    if not (
        type(claim) is FourSlotDispatchClaim
        and claim.signal_id == stage.signal_id
        and claim.external_oid == stage.external_oid
        and claim.state == "POST_POSSIBLE_UNRESOLVED"
        and claim.reconciliation_required is True
        and claim.post_attempted is False
        and claim.exchange_post_authorized is False
        and claim.live_authorization is False
        and claim.entry_allowed is False
        and claim.mexc_orders_sent is False
    ):
        raise V40DispatchHalted("V40_DISPATCH_CLAIM_RESULT_INVALID_NO_RETRY")

    try:
        order_id = await transport.submit_short_with_stop(
            intent, permit=permit,
        )
        if (
            type(order_id) is not str
            or re.fullmatch(r"[1-9][0-9]*", order_id) is None
        ):
            raise ValueError("ORDER_ACK_INVALID")
    except BaseException:
        # A POST can succeed despite an exception or missing HTTP response.
        # Try to journal UNKNOWN. Even if that DB write fails, the claim
        # persists: NO resubmission, no automatic close, no stop alteration.
        try:
            await journal.latch_unknown(
                signal_id=intent.signal_id,
                external_oid=intent.external_oid, lease=lease,
            )
        except Exception:
            raise V40DispatchHalted(
                "V40_POST_UNKNOWN_AND_UNKNOWN_LATCH_FAILED_NO_RETRY"
            ) from None
        raise V40DispatchHalted(
            "V40_POST_OUTCOME_UNKNOWN_NO_RETRY"
        ) from None

    # Never ACK from HTTP alone; later exact-origin signed GET + full
    # account/stop/filled-deal bookends and atomic DB commit are mandatory.
    return V40DispatchResult(
        status="V40_SINGLE_POST_ACK_UNPROTECTED_RECONCILE_ONLY",
        durable_claim_created=True,
        post_attempted=True,
        must_reconcile_external_oid=True,
    )
