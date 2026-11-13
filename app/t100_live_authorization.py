"""Typed fail-closed authorization boundary for T100 exchange mutations.

This is an accidental-use safety boundary, not a cryptographic capability.
A permit can be issued only from explicit live controls plus a complete set
of independently supplied release-gate evidence. Paper mode can never issue
a permit.

Issuing a permit does NOT submit an order and does not make any exchange call.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import t100_live_guards
from app.t100_live_guards import T100ExecutionControls


class LiveAuthorizationHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class LivePilotReadinessEvidence:
    code_revision_verified: bool
    strategy_parity_verified: bool
    durable_schema_deployed_verified: bool
    current_inventory_completeness_verified: bool
    account_reconciled: bool
    protected_position_soak_verified: bool
    restart_recovery_verified: bool
    native_stop_mutation_contract_verified: bool
    entry_reconciliation_verified: bool
    fill_and_accounting_verified: bool
    private_ws_pre_pilot_contract_verified: bool
    unresolved_mutations_absent: bool
    independent_review_completed: bool
    operator_tiny_pilot_authorized: bool

    def failures(self) -> tuple[str, ...]:
        mapping = (
            ("code_revision_unverified", self.code_revision_verified),
            ("strategy_parity_unverified", self.strategy_parity_verified),
            ("durable_schema_not_deployed", self.durable_schema_deployed_verified),
            (
                "current_inventory_completeness_unverified",
                self.current_inventory_completeness_verified,
            ),
            ("account_not_reconciled", self.account_reconciled),
            (
                "protected_position_soak_unverified",
                self.protected_position_soak_verified,
            ),
            ("restart_recovery_unverified", self.restart_recovery_verified),
            (
                "native_stop_mutation_contract_unverified",
                self.native_stop_mutation_contract_verified,
            ),
            (
                "entry_reconciliation_unverified",
                self.entry_reconciliation_verified,
            ),
            (
                "fill_and_accounting_unverified",
                self.fill_and_accounting_verified,
            ),
            (
                "private_ws_pre_pilot_contract_unverified",
                self.private_ws_pre_pilot_contract_verified,
            ),
            (
                "unresolved_mutations_present",
                self.unresolved_mutations_absent,
            ),
            (
                "independent_review_incomplete",
                self.independent_review_completed,
            ),
            (
                "operator_tiny_pilot_authorization_missing",
                self.operator_tiny_pilot_authorized,
            ),
        )
        return tuple(name for name, ok in mapping if ok is not True)


V40_CANARY_SCOPE = "T100_V40_SUPERVISED_FIRST_SIGNAL"


@dataclass(frozen=True)
class V40FirstCanaryReadinessEvidence:
    """First real-signal canary gates, deliberately NOT full autonomous gates.

    Actual filled-position stop execution, trailing, and restart behavior are
    what the first live test seeks to measure, so they are not pretended
    already proven. The independent guardian, native attached-SL API contract,
    durable journal and controlled emergency runbook must be verified FIRST.

    Callers must derive these fields from independent evidence. This class
    does not accept environment flags as proof and cannot certify anything.
    """
    code_revision_verified: bool
    frozen_strategy_parity_verified: bool
    immutable_journal_schema_and_grants_verified: bool
    genuine_fresh_one_signal_verified: bool
    current_exact_lot_and_depth_verified: bool
    dedicated_account_flat_and_hedge_verified: bool
    private_short_cross_one_x_verified: bool
    account_open_orders_and_unknown_claims_absent: bool
    native_attached_market_stop_request_contract_reviewed: bool
    post_fill_get_and_journal_reconciliation_reviewed: bool
    independent_nonflat_guardian_live_verified: bool
    durable_incident_alert_delivery_verified: bool
    supervised_emergency_exit_runbook_verified: bool
    operator_explicitly_authorized_first_canary: bool
    # The FIRST production trade is the intended experiment. The operator
    # explicitly accepts not having PRIOR nonflat/native-exit observations
    # and watches the independent guardian and event logs during the trade.
    # This never skips a native stop REQUEST, actual flat-account precheck,
    # exact signal/lot/price cap, order journal, or current guardian fence.
    production_first_trade_observation_waiver: bool = False

    def failures(self) -> tuple[str, ...]:
        names=(
            "code_revision_verified",
            "frozen_strategy_parity_verified",
            "immutable_journal_schema_and_grants_verified",
            "genuine_fresh_one_signal_verified",
            "current_exact_lot_and_depth_verified",
            "dedicated_account_flat_and_hedge_verified",
            "private_short_cross_one_x_verified",
            "account_open_orders_and_unknown_claims_absent",
            "native_attached_market_stop_request_contract_reviewed",
            "post_fill_get_and_journal_reconciliation_reviewed",
            "independent_nonflat_guardian_live_verified",
            "durable_incident_alert_delivery_verified",
            "supervised_emergency_exit_runbook_verified",
            "operator_explicitly_authorized_first_canary",
        )
        # There is no circular prerequisite to demonstrate a real exit
        # before the very first production trade. With the explicit
        # supervised observation waiver, only these two PREEXISTING
        # outcome/notification proofs are deferred to the live trial.
        waived=(
            frozenset({
                "independent_nonflat_guardian_live_verified",
                "durable_incident_alert_delivery_verified",
            })
            if self.production_first_trade_observation_waiver is True
               and self.operator_explicitly_authorized_first_canary is True
            else frozenset()
        )
        return tuple(
            "canary_"+name+"_missing"
            for name in names
            if name not in waived and getattr(self,name) is not True
        )


_PERMIT_SENTINEL = object()


@dataclass(frozen=True, init=False)
class LiveMutationPermit:
    max_position_notional_usdt: Decimal
    max_total_notional_usdt: Decimal
    max_daily_loss_usdt: Decimal
    leverage: int
    scope: str
    signal_id: int | None
    symbol: str | None

    def __init__(
        self,
        *,
        max_position_notional_usdt: Decimal,
        max_total_notional_usdt: Decimal,
        max_daily_loss_usdt: Decimal,
        leverage: int,
        scope: str,
        _sentinel: object,
        signal_id: int | None = None,
        symbol: str | None = None,
    ) -> None:
        if _sentinel is not _PERMIT_SENTINEL:
            raise LiveAuthorizationHalted(
                "LIVE_MUTATION_PERMIT_MUST_BE_ISSUED_BY_POLICY"
            )
        object.__setattr__(
            self,
            "max_position_notional_usdt",
            max_position_notional_usdt,
        )
        object.__setattr__(
            self,
            "max_total_notional_usdt",
            max_total_notional_usdt,
        )
        object.__setattr__(
            self,
            "max_daily_loss_usdt",
            max_daily_loss_usdt,
        )
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "symbol", symbol)


def issue_v40_supervised_canary_permit(
    controls: T100ExecutionControls,
    evidence: V40FirstCanaryReadinessEvidence,
    *, signal_id: int, symbol: str,
) -> LiveMutationPermit:
    """Mint a *signal-bound* first-live-canary permit; not autonomous mode.

    First live trade must not require retrospective successful stop/exit soak.
    It DOES require a separately running position guardian, durable incident
    alerts and operator-reviewed emergency procedure ahead of the attempt.
    No exchange call is made; the transport certification and one-shot
    journal/claim gates are still checked independently at POST.
    """
    import re
    if type(controls) is not T100ExecutionControls:
        raise LiveAuthorizationHalted("V40_CANARY_CONTROLS_INVALID")
    if type(evidence) is not V40FirstCanaryReadinessEvidence:
        raise LiveAuthorizationHalted("V40_CANARY_EVIDENCE_INVALID")
    failures=list(evidence.failures())
    if controls.mode!="live":
        failures.append("execution_mode_not_live")
    failures.extend(controls.readiness_failures())
    if (
        controls.leverage!=1
        or type(signal_id) is not int or signal_id<=0
        or type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT",symbol) is None
        or symbol in ("BTC_USDT","ETH_USDT")
    ):
        failures.append("V40_CANARY_SIGNAL_OR_CROSS_MODE_INVALID")
    budget=Decimal("12.50")
    for key in (
        "max_position_notional_usdt",
        "max_total_notional_usdt",
        "max_daily_loss_usdt",
    ):
        value=getattr(controls,key)
        if (
            type(value) is not Decimal or not value.is_finite()
            or not Decimal(0)<value<=budget
        ):
            failures.append("V40_CANARY_TINY_CAP_INVALID_"+key)
    if failures:
        raise LiveAuthorizationHalted(
            "V40_CANARY_AUTHORIZATION_BLOCKED:"+",".join(dict.fromkeys(failures))
        )
    return LiveMutationPermit(
        max_position_notional_usdt=controls.max_position_notional_usdt,
        max_total_notional_usdt=controls.max_total_notional_usdt,
        max_daily_loss_usdt=controls.max_daily_loss_usdt,
        leverage=1,scope=V40_CANARY_SCOPE,
        signal_id=signal_id,symbol=symbol,
        _sentinel=_PERMIT_SENTINEL,
    )


def issue_live_mutation_permit(
    controls: T100ExecutionControls,
    evidence: LivePilotReadinessEvidence,
) -> LiveMutationPermit:
    if not isinstance(controls, T100ExecutionControls):
        raise LiveAuthorizationHalted("LIVE_CONTROLS_INVALID")
    if not isinstance(evidence, LivePilotReadinessEvidence):
        raise LiveAuthorizationHalted("LIVE_READINESS_EVIDENCE_INVALID")

    failures: list[str] = []
    if controls.mode != "live":
        failures.append("execution_mode_not_live")
    failures.extend(controls.readiness_failures())
    failures.extend(evidence.failures())

    if controls.leverage != 1:
        failures.append("tiny_pilot_requires_1x_leverage")
    if (
        controls.max_position_notional_usdt is None
        or controls.max_total_notional_usdt is None
        or controls.max_daily_loss_usdt is None
    ):
        # readiness_failures already reports these, but keep construction
        # impossible even if that method changes later.
        failures.append("live_risk_limits_incomplete")

    if failures:
        raise LiveAuthorizationHalted(
            "LIVE_MUTATION_AUTHORIZATION_BLOCKED:"
            + ",".join(dict.fromkeys(failures))
        )

    return LiveMutationPermit(
        max_position_notional_usdt=controls.max_position_notional_usdt,
        max_total_notional_usdt=controls.max_total_notional_usdt,
        max_daily_loss_usdt=controls.max_daily_loss_usdt,
        leverage=controls.leverage,
        scope="T100_TINY_PILOT",
        _sentinel=_PERMIT_SENTINEL,
    )


def require_live_mutation_permit(
    permit: LiveMutationPermit,
) -> LiveMutationPermit:
    if not isinstance(permit, LiveMutationPermit):
        raise LiveAuthorizationHalted("LIVE_MUTATION_PERMIT_REQUIRED")
    if (
        permit.scope not in ("T100_TINY_PILOT",V40_CANARY_SCOPE)
        or permit.leverage != 1
        or any(
            type(value) is not Decimal
            or not value.is_finite()
            or value <= 0
            for value in (
                permit.max_position_notional_usdt,
                permit.max_total_notional_usdt,
                permit.max_daily_loss_usdt,
            )
        )
        or permit.max_position_notional_usdt > permit.max_total_notional_usdt
    ):
        raise LiveAuthorizationHalted("LIVE_MUTATION_PERMIT_INVALID")
    if permit.scope==V40_CANARY_SCOPE:
        import re
        if (
            any(value>Decimal("12.50") for value in (
                permit.max_position_notional_usdt,
                permit.max_total_notional_usdt,
                permit.max_daily_loss_usdt,
            ))
            or type(permit.signal_id) is not int or permit.signal_id<=0
            or type(permit.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT",permit.symbol) is None
            or permit.symbol in ("BTC_USDT","ETH_USDT")
        ):
            raise LiveAuthorizationHalted("V40_CANARY_SIGNAL_BOUND_PERMIT_INVALID")
    elif permit.signal_id is not None or permit.symbol is not None:
        raise LiveAuthorizationHalted("LIVE_LEGACY_PERMIT_CANNOT_PIN_CANARY")
    return permit


def require_certified_transport_permit(
    permit: LiveMutationPermit,
) -> LiveMutationPermit:
    """Guard the final transport boundary, independent of caller evidence.

    An offline test can simulate a certified adapter by patching the module
    constant, but a production adapter remains blocked while the repository's
    status is NOT_IMPLEMENTED. This does not certify the adapter or itself
    establish live permissions, leader fencing, or current account readiness.
    """
    checked = require_live_mutation_permit(permit)
    if checked.scope==V40_CANARY_SCOPE:
        # This code status is intentionally NOT_IMPLEMENTED until the
        # exchange/native-stop and independent guardian release is reviewed.
        # Do NOT allow an environment variable to override certification.
        if t100_live_guards.LIVE_ADAPTER_STATUS!="V40_CANARY_RESTRICTED":
            raise LiveAuthorizationHalted("V40_CANARY_ADAPTER_NOT_RELEASED")
    elif t100_live_guards.LIVE_ADAPTER_STATUS!="CERTIFIED":
        raise LiveAuthorizationHalted("LIVE_ADAPTER_NOT_CERTIFIED")
    return checked
