"""One-shot V40 entry coordinator: Stage2 -> account plan -> durable POST claim.

This is the missing composition of *existing* durable building blocks, not a
new exchange transport. It is deliberately NOT registered as a Render
worker. No background loop or retry exists. The actual shipped adapter
remains NOT_IMPLEMENTED, so production calls fail BEFORE any DB mutation.

A future deploy must inject independently verified account risk, full
parity/report evidence, exchange contract terms, restricted SQL roles,
native stop protection and crash/restart reconciliation.

Timeline:
    require issued/certified permit + full strategy parity BEFORE writes;
    read Stage2 source; acquire fenced lease; GET/SQL account snapshot;
    read independent market/collateral economics; plan frozen V40 single bar;
    derive exact executable-lot intent; reserve SQL slot;
    atomically stage PREPARED->SUBMITTING; re-observe current economics;
    irreversible SQL claim; ONE market order with native SL;
    GET exact origin, fills and native SL; commit protected checkpoint;
    fresh independent restart/recovery witness before further entry.

After SQL reservation/stage, errors are not automatically undone. A caller
must investigate and reconcile; there is NO retry to an ambiguous exchange
POST, and no self-certification of exchange/DB atomicity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal as D
import time
from typing import Any, Callable, Protocol, Sequence

from app.t100_live_authorization import (
    LiveMutationPermit, require_certified_transport_permit,
)
from app.t100_live_execution import (
    EntryIntent, LiveCaps, modeled_short_loss_at_native_stop,
)
from app.t100_live_leader_fencing import (
    LeaderLease, LeaderLeaseCoordinator,
)
from app.t100_live_four_slot_planner import (
    FourSlotPlan, plan_four_slot_v40,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, FourSlotSnapshotCoordinator,
)
from app.t100_live_four_slot_reservations import (
    PostgresFourSlotReservations, SlotReservation,
)
from app.t100_live_four_slot_attempt_journal import (
    PostgresFourSlotAttemptJournal, DurableAttemptStage,
)
from app.t100_live_v40_dispatch_executor import (
    V40CurrentRiskEvidence, dispatch_one_staged_v40,
)
from app.t100_live_v40_post_dispatch_reconcile import (
    reconcile_and_commit_protected_v40,
)
from app.t100_live_strategy_parity_gate import (
    assess_strategy_parity_report,
    assess_v40_first_canary_entry_parity_report,
)
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source, V40SourceCandidate, V40SourceBatch,
)


class V40CoordinatorHalted(RuntimeError):
    """Stable code only, no account identifiers or raw exceptions."""


@dataclass(frozen=True)
class V40ContractTerms:
    """Current independently collected MEXC terms; values alone prove nothing."""
    contract_size: D
    volume_step: D
    min_volume: D
    max_volume: D
    price_tick: D
    private_order_permission_verified: bool = False
    market_state_and_country_verified: bool = False

    def validate(self) -> None:
        if (
            any(type(x) is not D or not x.is_finite() or x <= 0
                for x in (
                    self.contract_size,self.volume_step,self.min_volume,
                    self.max_volume,self.price_tick,
                ))
            or self.max_volume < self.min_volume
            or self.private_order_permission_verified is not True
            or self.market_state_and_country_verified is not True
        ):
            raise V40CoordinatorHalted("V40_CURRENT_CONTRACT_TERMS_UNVERIFIED")


@dataclass(frozen=True)
class V40PreparedEntry:
    signal: V40SourceCandidate
    plan: FourSlotPlan
    intent: EntryIntent
    terms: V40ContractTerms
    # Preparation is NOT an order permit, including when action=ADMIT.
    entry_allowed: bool = False
    live_authorization: bool = False


@dataclass(frozen=True)
class V40OneShotReport:
    status: str
    signal_id: int | None = None
    reserved: bool = False
    staged: bool = False
    post_attempted: bool = False
    claim_may_exist: bool = False
    independently_recovered_after_commit: bool = False
    # This outcome NEVER certifies the entire autonomous V40 service.
    entry_allowed: bool = False
    live_authorization: bool = False


class AccountAndContractWitness(Protocol):
    async def inspect(
        self, signal: V40SourceCandidate,
    ) -> tuple[V40CurrentRiskEvidence,V40ContractTerms]: ...


class ExactMarketDepthWitness(Protocol):
    async def quote_exact_intent(
        self, signal: V40SourceCandidate, intent: EntryIntent,
        *, max_slippage_bps: D,
    ) -> tuple[Any,Any]: ...


def verify_fresh_exact_depth(
    *,
    signal: V40SourceCandidate,
    intent: EntryIntent,
    risk: V40CurrentRiskEvidence,
    terms: V40ContractTerms,
    caps: LiveCaps,
    quote: Any,
    depth: Any,
    now_ms: int,
) -> V40CurrentRiskEvidence:
    """Merge ONLY verified sized VWAP/age; never upgrade permission flags.

    A top-of-book price is insufficient. Validate both independent sources
    agree on economics and immutable lot grid before any dispatch claim.
    Strictly isolated FLAT canary only until non-flat depth is supported.
    """
    # Local import breaks the module dependency cycle: the standalone
    # GET-only market witness reuses this coordinator's V40ContractTerms.
    from app.t100_live_v40_candidate_market_witness import (
        V40CandidateQuote, V40ExactDepth,
    )
    if not (
        type(signal) is V40SourceCandidate
        and type(intent) is EntryIntent
        and type(risk) is V40CurrentRiskEvidence
        and type(terms) is V40ContractTerms
        and type(caps) is LiveCaps
        and type(quote) is V40CandidateQuote
        and type(depth) is V40ExactDepth
        and type(now_ms) is int
        and now_ms > 0
    ):
        raise V40CoordinatorHalted("V40_EXACT_DEPTH_SOURCE_INVALID")

    qt=quote.candidate_contract
    grid=("contract_size","volume_step","min_volume","max_volume","price_tick")
    if not (
        quote.status=="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
        and depth.status=="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING"
        and quote.symbol==depth.symbol==signal.symbol==intent.symbol
        and depth.signal_id==intent.signal_id==signal.signal_id
        and quote.bookended_flat_account is True
        and quote.hedge_mode_observed is True
        and quote.private_risk_row_observed is True
        and depth.order_fits_depth is True
        and quote.entry_allowed is False
        and quote.live_authorization is False
        and depth.entry_allowed is False
        and depth.live_authorization is False
        and type(qt) is V40ContractTerms
        and all(getattr(qt,field)==getattr(terms,field) for field in grid)
        and qt.market_state_and_country_verified is True
        and risk.current_owned_positions==0
        and all(type(value) is D and value.is_finite() and value>0 for value in (
            intent.volume_contracts,intent.notional_usdt,
            risk.current_account_equity_usdt,quote.account_equity_usdt,
            quote.current_private_short_max_vol,depth.sell_vwap,
        ))
        and type(quote.account_available_usdt) is D
        and quote.account_available_usdt.is_finite()
        and quote.account_available_usdt>=0
        and type(risk.available_margin_usdt) is D
        and risk.available_margin_usdt.is_finite()
        and risk.available_margin_usdt>=0
        and quote.account_equity_usdt==risk.current_account_equity_usdt
        and risk.available_margin_usdt<=quote.account_available_usdt
        and intent.volume_contracts==depth.contracts
        and intent.volume_contracts<=quote.current_private_short_max_vol
        and intent.volume_contracts<=terms.max_volume
        and intent.volume_contracts>=terms.min_volume
        and intent.volume_contracts%terms.volume_step==0
        and intent.notional_usdt*D("1.02")<=quote.account_available_usdt
        and type(depth.adverse_slippage_bps) is D
        and depth.adverse_slippage_bps.is_finite()
        and depth.adverse_slippage_bps<=caps.max_slippage_bps
    ):
        raise V40CoordinatorHalted("V40_EXACT_DEPTH_OR_ACCOUNT_DRIFT_NO_POST")

    # The depth was sampled before any subsequent claim; check at the
    # final boundary with an independent monotonic-now wall clock.
    if not (
        type(quote.observed_at_ms) is int
        and type(quote.book_age_ms) is int
        and type(depth.age_ms) is int
        and 0<=quote.book_age_ms==depth.age_ms<=caps.max_book_age_ms
        and 0<=now_ms-quote.observed_at_ms<=caps.max_book_age_ms
    ):
        raise V40CoordinatorHalted("V40_EXACT_DEPTH_TOO_OLD_NO_POST")

    # This does NOT prove full cross-margin collateral or private key POST
    # privilege, and cannot grant those permissions. Never copy or flip
    # any of the original independent risk certification booleans.
    return dataclass_replace(
        risk,observed_sell_vwap=depth.sell_vwap,
        book_age_ms=max(
            quote.book_age_ms,depth.age_ms,
            now_ms-quote.observed_at_ms,
        ),
    )


def prepare_candidate(
    *,
    signal: V40SourceCandidate,
    witness: FourSlotSnapshotResult,
    risk: V40CurrentRiskEvidence,
    terms: V40ContractTerms,
    cost_bps: float,
    pilot_notional_ceiling: D | None = None,
) -> V40PreparedEntry | None:
    """No DB mutation or transport; exact frozen V40 quantities and origin."""
    if (
        type(signal) is not V40SourceCandidate
        or type(witness) is not FourSlotSnapshotResult
        or type(risk) is not V40CurrentRiskEvidence
        or type(terms) is not V40ContractTerms
        or type(cost_bps) not in (int,float)
        or cost_bps not in (100.,200.)
        or signal.entry_allowed is not False
        or signal.live_authorization is not False
        or witness.status not in (
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
    ):
        raise V40CoordinatorHalted("V40_ENTRY_PLANNING_EVIDENCE_INVALID")
    terms.validate()
    if (
        type(risk.current_account_equity_usdt) is not D
        or not risk.current_account_equity_usdt.is_finite()
        or risk.current_account_equity_usdt <= 0
        or type(risk.portfolio_notional_usdt) is not D
        or not risk.portfolio_notional_usdt.is_finite()
        or risk.portfolio_notional_usdt < 0
        or type(risk.current_owned_positions) is not int
        or risk.current_owned_positions != witness.occupied_slots
        or risk.inventory_bookended_complete is not True
        or risk.no_unowned_positions_or_orders is not True
        or risk.cross_collateral_verified is not True
        or risk.private_risk_tier_verified is not True
        or risk.hedge_mode_verified is not True
        or risk.contract_trading_access_verified is not True
        or risk.no_unresolved_mutations is not True
    ):
        raise V40CoordinatorHalted("V40_CURRENT_ACCOUNT_WITNESS_INCOMPLETE")
    plan=plan_four_slot_v40(
        inventory=witness.inventory,durable_recovery=witness.recovery,
        symbol=signal.symbol,prior_bars=signal.prior_bars,
        admission_open_ts=signal.admission_open_ts,
        stage1_atr7_pct=signal.stage1_atr7_pct,
        equity_usdt=float(risk.current_account_equity_usdt),
        marked_existing_usdt=float(risk.portfolio_notional_usdt),
        cost_bps=float(cost_bps),
    )
    if plan.action in ("REJECT","SKIP"):
        return None
    if (
        plan.action != "ADMIT"
        or plan.candidate_symbol != signal.symbol
        or plan.entry_allowed is not False
        or plan.live_authorization is not False
    ):
        raise V40CoordinatorHalted("V40_FOUR_SLOT_ACCOUNT_OR_STRATEGY_BLOCKED")
    if pilot_notional_ceiling is not None and (
        type(pilot_notional_ceiling) is not D
        or not pilot_notional_ceiling.is_finite()
        or pilot_notional_ceiling <= 0
    ):
        raise V40CoordinatorHalted("V40_PILOT_CEILING_INVALID")
    capped_target=D(str(plan.modeled_notional_usdt))
    if pilot_notional_ceiling is not None:
        capped_target=min(capped_target,pilot_notional_ceiling)
    try:
        intent=EntryIntent.build(
            signal_id=signal.signal_id,symbol=signal.symbol,
            reference_price=D(str(signal.entry_reference_price)),
            notional_usdt=capped_target,
            contract_size=terms.contract_size,
            volume_step=terms.volume_step,
            min_volume=terms.min_volume,
            max_volume=terms.max_volume,
            price_tick=terms.price_tick,
        )
    except (ValueError,ArithmeticError):
        # Real lot constraints are stricter than research's reference
        # fractional-size model. Never invent an executable minimum.
        raise V40CoordinatorHalted(
            "V40_CONTRACT_LOT_CANNOT_EXECUTE_MODELED_SIZE"
        ) from None
    if (
        intent.notional_usdt <= 0
        or intent.notional_usdt > D(str(plan.modeled_notional_usdt))
        or intent.notional_usdt > capped_target
    ):
        raise V40CoordinatorHalted("V40_ORDER_EXCEEDS_MODEL_NOTIONAL")
    return V40PreparedEntry(signal,plan,intent,terms)


class V40OneShotCoordinator:
    """Single attempt; assumes no previously unclaimed or unknown POST.

    Every injected collaborator must be independently reviewed for
    permissions, financial data provenance and current real-venue behavior.
    No persistent scheduler is installed by this module.
    """

    def __init__(
        self, *,
        source: PostgresV40Stage2Source,
        snapshots: FourSlotSnapshotCoordinator,
        witness: AccountAndContractWitness,
        depth: ExactMarketDepthWitness,
        leaders: LeaderLeaseCoordinator,
        reservations: PostgresFourSlotReservations,
        journal: PostgresFourSlotAttemptJournal,
        claims: Any,
        claim_reader: Any,
        transport: Any,
        get_venue: Any,
        completeness: Any,
        commit_writer: Any,
        clock_ms: Callable[[],int] | None = None,
        initial_pilot_only: bool = False,
    ) -> None:
        if depth is None:
            raise ValueError("V40_EXACT_DEPTH_WITNESS_REQUIRED")
        if type(initial_pilot_only) is not bool:
            raise ValueError("V40_INITIAL_PILOT_SCOPE_INVALID")
        self.source=source
        self.snapshots=snapshots
        self.witness=witness
        self.depth=depth
        self.clock_ms=clock_ms if clock_ms is not None else lambda: int(time.time()*1000)
        self.leaders=leaders
        self.reservations=reservations
        self.journal=journal
        self.claims=claims
        self.claim_reader=claim_reader
        self.transport=transport
        self.get_venue=get_venue
        self.completeness=completeness
        self.commit_writer=commit_writer
        self.initial_pilot_only=initial_pilot_only

    async def run_one(
        self, *,
        permit: LiveMutationPermit,
        caps: LiveCaps,
        parity_report: dict[str,Any],
        holder_id: str,
        cost_bps: float = 100.,
        monitored_signal_id: int | None = None,
        monitored_symbol: str | None = None,
        monitored_tier: str | None = None,
    ) -> V40OneShotReport:
        # Absolutely no PG writes, API GETs, lease renewal or POST before
        # independently issued permit and full historical strategy gate.
        require_certified_transport_permit(permit)
        # The supervised-first-signal permit is NOT an autonomous feed
        # selector. It may only handle exactly its own operator-approved
        # genuine Stage2 identity and first-position flat-only mode.
        from app.t100_live_authorization import V40_CANARY_SCOPE
        if getattr(permit,'scope',None)==V40_CANARY_SCOPE and (
            self.initial_pilot_only is not True
            or monitored_signal_id!=permit.signal_id
            or monitored_symbol!=permit.symbol
            or monitored_tier not in ("LOW_100","HIGH_225")
        ):
            raise V40CoordinatorHalted(
                "V40_CANARY_PINNED_REAL_SIGNAL_AND_SINGLE_POSITION_REQUIRED"
            )
        if type(caps) is not LiveCaps:
            raise V40CoordinatorHalted("V40_OPERATOR_RISK_CAPS_REQUIRED")
        # ONE capped, supervised first real-money canary tests venue
        # execution/SL/recovery; it cannot be required to prove outcomes
        # before its first trade. Keep full replay compulsory for legacy
        # and autonomous scopes, while retaining exact frozen entry parity.
        if getattr(permit,"scope",None)==V40_CANARY_SCOPE:
            parity=assess_v40_first_canary_entry_parity_report(parity_report)
            if parity["first_canary_entry_parity_verified"] is not True:
                raise V40CoordinatorHalted(
                    "V40_FIRST_CANARY_FROZEN_ENTRY_PARITY_NOT_VERIFIED"
                )
        else:
            parity=assess_strategy_parity_report(parity_report)
            if parity.get("strategy_parity_verified") is not True:
                raise V40CoordinatorHalted("V40_FULL_STRATEGY_PARITY_NOT_VERIFIED")
        now=datetime.now(timezone.utc)
        tape=await self.source.latest_candidates(observed_at=now)
        if (
            type(tape) is not V40SourceBatch
            or tape.status!="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
            or tape.read_only_repeatable_read is not True
            or tape.entry_allowed is not False
            or tape.live_authorization is not False
        ):
            raise V40CoordinatorHalted("V40_SOURCE_BATCH_UNVERIFIED")
        # A monitor-originated handoff must pin the exact real source row.
        # Never substitute another (even admissible) candidate if the tape
        # changed between the read-only monitor and the one-shot executor.
        # This check is BEFORE lease, reservation, claim, GET or exchange POST.
        monitored = (
            monitored_signal_id is not None
            or monitored_symbol is not None
            or monitored_tier is not None
        )
        if monitored:
            if (
                type(monitored_signal_id) is not int
                or monitored_signal_id <= 0
                or type(monitored_symbol) is not str
                or not monitored_symbol.endswith("_USDT")
                or type(monitored_tier) is not str
                or monitored_tier not in ("LOW_100", "HIGH_225")
            ):
                raise V40CoordinatorHalted("V40_MONITORED_SIGNAL_IDENTITY_INVALID")
            if (
                len(tape.candidates) != 1
                or tape.candidates[0].signal_id != monitored_signal_id
                or tape.candidates[0].symbol != monitored_symbol
                or tape.candidates[0].tier != monitored_tier
            ):
                raise V40CoordinatorHalted("V40_MONITORED_SIGNAL_CHANGED_OR_NOT_UNIQUE")
        if not tape.candidates:
            return V40OneShotReport("V40_NO_CURRENT_STAGE2_SIGNALS")

        # Exactly one fenced executor, *before* selecting the account.
        lease=await self.leaders.acquire(holder_id)
        selected: V40PreparedEntry | None=None
        pre: FourSlotSnapshotResult | None=None
        # A reservation INSERT can commit before its network ACK arrives.
        # Once attempted, avoid voluntarily releasing the leader fence;
        # expiry and recovery, never automatic resubmission, follow.
        reservation_mutation_may_exist=False
        try:
            pre=await self.snapshots.inspect()
            if type(pre) is not FourSlotSnapshotResult or pre.status not in (
                "FLAT_OBSERVED_NONAUTHORIZING",
                "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
            ):
                raise V40CoordinatorHalted("V40_CRITICAL_ACCOUNT_SNAPSHOT_BLOCKED")
            # First real-money V40 canary is strictly one FLAT-to-one SHORT.
            # The four-slot research plan is not a pilot authorization.
            # Existing offline multi-slot tests retain the legacy default.
            if self.initial_pilot_only and (
                pre.status!="FLAT_OBSERVED_NONAUTHORIZING"
                or pre.occupied_slots!=0
            ):
                raise V40CoordinatorHalted(
                    "V40_INITIAL_PILOT_REQUIRES_FLAT_ACCOUNT_NO_LEGACY_EXPOSURE"
                )
            for signal in tape.candidates:
                risk, terms=await self.witness.inspect(signal)
                # Tiny real-money pilot is an explicit capped-size
                # execution variation, not a replacement for the frozen
                # V40 research model. Do not round UP to exchange minimum.
                pilot_ceiling=None
                if caps.max_order_usdt<=D("12.50"):
                    permit_cap=getattr(
                        permit,"max_position_notional_usdt",
                        caps.max_order_usdt,
                    )
                    if (
                        type(risk) is not V40CurrentRiskEvidence
                        or type(permit_cap) is not D
                        or type(risk.current_account_equity_usdt) is not D
                        or type(risk.available_margin_usdt) is not D
                    ):
                        raise V40CoordinatorHalted("V40_PILOT_RISK_BUDGET_UNVERIFIED")
                    pilot_ceiling=min(
                        caps.max_order_usdt,permit_cap,
                        risk.current_account_equity_usdt*D("0.25"),
                        risk.available_margin_usdt,
                    )
                    if not pilot_ceiling.is_finite() or pilot_ceiling<=0:
                        return V40OneShotReport(
                            "V40_PILOT_RISK_BUDGET_EMPTY_NO_RESERVATION",
                            signal_id=signal.signal_id,
                        )
                selected=prepare_candidate(
                    signal=signal,witness=pre,risk=risk,
                    terms=terms,cost_bps=cost_bps,
                    pilot_notional_ceiling=pilot_ceiling,
                )
                if selected is not None:
                    break
            if selected is None:
                return V40OneShotReport("V40_NO_ADMISSIBLE_STAGE2_CANDIDATE")
            # Refuse predictably over-budget entries BEFORE durable reservation.
            # The frozen research target is not permission to exceed the
            # operator's small pilot ceiling. No top-up to min exchange lot.
            # Live permits are independently checked at entry; the test
            # harness injects a guarded fake without a permit instance.
            permit_order_cap=getattr(
                permit,"max_position_notional_usdt",caps.max_order_usdt,
            )
            if (
                type(permit_order_cap) is not D
                or selected.intent.notional_usdt > min(
                    caps.max_order_usdt,permit_order_cap,
                )
            ):
                return V40OneShotReport(
                    "V40_PRE_RESERVATION_PILOT_NOTIONAL_CAP_REJECTED",
                    signal_id=selected.signal.signal_id,
                )
            # Model the frozen +75% SHORT stop before any durable claim.
            # This is a risk-budget refusal, never a guaranteed max loss:
            # venue gaps, liquidation, funding and failed stops are separate.
            try:
                modeled_loss=modeled_short_loss_at_native_stop(
                    selected.intent,max_adverse_slippage_bps=caps.max_slippage_bps,
                )
                daily_cap=min(
                    caps.max_daily_loss_usdt,
                    getattr(permit,"max_daily_loss_usdt",caps.max_daily_loss_usdt),
                )
                pre_reservation_loss_ok=(
                    type(daily_cap) is D
                    and type(risk.realized_today_loss_usdt) is D
                    and risk.realized_today_loss_usdt.is_finite()
                    and risk.realized_today_loss_usdt>=0
                    and risk.realized_today_loss_usdt+modeled_loss<daily_cap
                )
            except Exception:
                pre_reservation_loss_ok=False
            if not pre_reservation_loss_ok:
                return V40OneShotReport(
                    "V40_PRE_RESERVATION_MODELED_SL75_LOSS_CAP_REJECTED",
                    signal_id=selected.signal.signal_id,
                )
            # Fresh lease and critical account after GET-heavy selection.
            await self.leaders.require_current(lease)
            observed_at=datetime.now(timezone.utc)
            reservation_mutation_may_exist=True
            reserved=await self.reservations.reserve(
                plan=selected.plan,witness=pre,lease=lease,
                observed_at=observed_at,
                signal_id=selected.signal.signal_id,
                symbol=selected.signal.symbol,
            )
            # No POST and no automatic retry after *any* subsequent
            # reservation or staging ambiguity.
            if (
                type(reserved) is not SlotReservation
                or reserved.external_oid!=selected.intent.external_oid
                or reserved.signal_id!=selected.signal.signal_id
                or reserved.symbol!=selected.signal.symbol
            ):
                return V40OneShotReport(
                    "V40_RESERVED_ORIGIN_MISMATCH_MANUAL_RECOVERY_ONLY",
                    signal_id=selected.signal.signal_id,
                    reserved=True,
                )
            stage=await self.journal.stage(
                intent=selected.intent,witness=pre,lease=lease,
                observed_at=observed_at,
                contract_size=selected.terms.contract_size,
                volume_step=selected.terms.volume_step,
                price_tick=selected.terms.price_tick,
            )
            if (
                type(stage) is not DurableAttemptStage
                or stage.external_oid!=selected.intent.external_oid
                or stage.signal_id!=selected.signal.signal_id
            ):
                return V40OneShotReport(
                    "V40_STAGED_ORIGIN_UNVERIFIED_RECONCILIATION_ONLY",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )

            # Re-read live depth/collateral before any possible entry,
            # preserving immutable prepared lots and original signal.
            refreshed, terms_new=await self.witness.inspect(selected.signal)
            if terms_new!=selected.terms:
                return V40OneShotReport(
                    "V40_CONTRACT_TERMS_CHANGED_AFTER_STAGE_NO_POST",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )
            # Recalculate frozen V40 admission using *current* equity and
            # marked exposure; the earlier reservation does NOT entitle us
            # to the old notional after adverse portfolio changes.
            if type(refreshed) is not V40CurrentRiskEvidence:
                return V40OneShotReport(
                    "V40_REFRESHED_RISK_EVIDENCE_INVALID_NO_POST",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )
            try:
                revised_ceiling=pilot_ceiling
                if pilot_ceiling is not None:
                    revised_ceiling=min(
                        pilot_ceiling,
                        refreshed.current_account_equity_usdt*D("0.25"),
                        refreshed.available_margin_usdt,
                    )
                revised=prepare_candidate(
                    signal=selected.signal,witness=pre,
                    risk=refreshed,terms=terms_new,cost_bps=cost_bps,
                    pilot_notional_ceiling=revised_ceiling,
                )
            except V40CoordinatorHalted:
                revised=None
            if (
                revised is None
                or selected.intent.notional_usdt >
                   revised.intent.notional_usdt
            ):
                return V40OneShotReport(
                    "V40_REFRESHED_V40_CAPACITY_OR_COLLATERAL_REJECTS_POST",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )
            # The exact *already staged* contract quantity is quoted on
            # fresh MEXC signed/public GET data, never substituted with
            # the previous top-of-book bid. The source currently requires
            # a completely flat account and never certifies authority.
            # No new SQL mutation or exchange POST is allowed if the
            # quantity-sized quote cannot be independently verified.
            try:
                quote,depth = await self.depth.quote_exact_intent(
                    selected.signal,selected.intent,
                    max_slippage_bps=caps.max_slippage_bps,
                )
                sized_risk=verify_fresh_exact_depth(
                    signal=selected.signal,intent=selected.intent,
                    risk=refreshed,terms=selected.terms,caps=caps,
                    quote=quote,depth=depth,now_ms=self.clock_ms(),
                )
            except Exception:
                return V40OneShotReport(
                    "V40_EXACT_EXECUTABLE_DEPTH_OR_ACCOUNT_UNVERIFIED_FREEZE",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )
            # After SUBMITTING, a lost lease is an ambiguity requiring
            # reconciliation; NEVER run a second entry attempt.
            await self.leaders.renew(lease)
            # Revalidate age at the *last* no-write boundary. If the
            # fencing/renew path took too long, never claim a new POST.
            try:
                sized_risk=verify_fresh_exact_depth(
                    signal=selected.signal,intent=selected.intent,
                    risk=refreshed,terms=selected.terms,caps=caps,
                    quote=quote,depth=depth,now_ms=self.clock_ms(),
                )
            except V40CoordinatorHalted:
                return V40OneShotReport(
                    "V40_EXACT_EXECUTABLE_DEPTH_STALE_AFTER_LEASE_FREEZE",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                )
            # The scanner may have invalidated the Stage2 entry while
            # the leader, SQL stage and multi-bookend MEXC GETs ran. The
            # first real canary MUST re-read the same genuine source row
            # immediately before the irreversible exchange dispatch claim.
            # Never substitute a new candidate or send a late POST. This
            # second read is restricted READ ONLY; a decline leaves the
            # staged identity for explicit reconciliation, not retry.
            if self.initial_pilot_only:
                try:
                    final_tape=await self.source.latest_candidates(
                        observed_at=datetime.now(timezone.utc),
                    )
                    identity=(
                        selected.signal.signal_id,
                        selected.signal.symbol,
                        selected.signal.tier,
                        selected.signal.admission_open_ts,
                        selected.signal.entry_reference_price,
                    )
                    final_valid=(
                        type(final_tape) is V40SourceBatch
                        and final_tape.status==
                            "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
                        and final_tape.read_only_repeatable_read is True
                        and final_tape.entry_allowed is False
                        and final_tape.live_authorization is False
                        and len(final_tape.candidates)==1
                        and type(final_tape.candidates[0]) is V40SourceCandidate
                        and (
                            final_tape.candidates[0].signal_id,
                            final_tape.candidates[0].symbol,
                            final_tape.candidates[0].tier,
                            final_tape.candidates[0].admission_open_ts,
                            final_tape.candidates[0].entry_reference_price,
                        )==identity
                        and 0<=final_tape.candidates[0].observed_age_seconds<=900
                    )
                except Exception:
                    final_valid=False
                if not final_valid:
                    return V40OneShotReport(
                        "V40_FINAL_STAGE2_CHANGED_OR_EXPIRED_NO_POST_RECONCILE",
                        signal_id=selected.signal.signal_id,
                        reserved=True,staged=True,
                        claim_may_exist=False,
                    )
            dispatch=await dispatch_one_staged_v40(
                plan=selected.plan,intent=selected.intent,stage=stage,
                lease=lease,risk=sized_risk,caps=caps,permit=permit,
                price_tick=selected.terms.price_tick,
                parity_report=parity_report,
                claims=self.claims,journal=self.journal,
                transport=self.transport,
            )
            if not dispatch.must_reconcile_external_oid:
                return V40OneShotReport(
                    "V40_DISPATCH_UNVERIFIED_RECONCILIATION_ONLY",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                    claim_may_exist=True,
                )
            report=await reconcile_and_commit_protected_v40(
                stages=(stage,),intents=(selected.intent,),
                pre_attempt_witness=pre,claim_reader=self.claim_reader,
                venue=self.get_venue,completeness=self.completeness,
                commit_writer=self.commit_writer,
                lease=lease,observed_at=datetime.now(timezone.utc),
            )
            if report.status != (
                "V40_PROTECTED_FILL_AND_CHECKPOINT_PERSISTED_RESTART_REQUIRED"
            ):
                return V40OneShotReport(
                    "V40_POST_SUBMISSION_PROTECTION_OR_DB_UNVERIFIED_FREEZE",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,
                    post_attempted=True,claim_may_exist=True,
                )
            # A new full-account GET/ledger bookend is required *after*
            # atomic fill+checkpoint persistence.
            recovered=await self.snapshots.inspect()
            if (
                type(recovered) is not FourSlotSnapshotResult
                or recovered.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
                or recovered.recovery is None
                or selected.signal.symbol not in {
                    p.symbol for p in recovered.recovery.recovered
                }
            ):
                return V40OneShotReport(
                    "V40_CHECKPOINT_COMMITTED_RESTART_UNVERIFIED_FREEZE",
                    signal_id=selected.signal.signal_id,
                    reserved=True,staged=True,post_attempted=True,
                    claim_may_exist=True,
                )
            return V40OneShotReport(
                "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
                signal_id=selected.signal.signal_id,
                reserved=True,staged=True,post_attempted=True,
                claim_may_exist=True,
                independently_recovered_after_commit=True,
            )
        finally:
            # Do not voluntarily unlock after a possibly durable reservation:
            # an UNKNOWN DB reply can have committed; an operator/recovery
            # process must reconcile that immutable identity. The leader
            # lease expires automatically without being falsely renewed.
            if not reservation_mutation_may_exist:
                try:
                    await self.leaders.release(lease)
                except Exception:
                    pass
