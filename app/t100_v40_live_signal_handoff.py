"""Strict handoff from a real V40 read-only signal monitor to a one-shot executor.

Not deployed, not an exchange transport, and not an authority to trade.
The running Render monitor remains GET/SELECT-only and paper-kill-latched.

The monitor can report a genuine signal but it cannot certify stop lifecycles,
independent nonflat protection or runtime adapter readiness. This separate
boundary refuses an unready monitor, then pins the identical signal ID,
symbol and tier at the independent coordinator's SECOND Stage2 read.
No fallback signal, implicit promotion, repeated POST or autonomous retry.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.t100_live_authorization import (
    LiveMutationPermit, require_certified_transport_permit,
)
from app.t100_live_execution import LiveCaps
from app.t100_live_v40_entry_coordinator import V40OneShotReport
from app.t100_v40_render_supervised_pilot_worker import (
    V40SupervisedPilotObservation,
)

FIRST_JOURNAL_STATUS = "V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED"
FRESH_SIGNAL_STATUS = "V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED"
PILOT_MAX_NOTIONAL_USDT = Decimal("12.50")
PILOT_MAX_EQUITY_FRACTION = Decimal("0.25")


class MonitoredOneShotExecutor(Protocol):
    async def run_one(
        self, *, permit: LiveMutationPermit, caps: LiveCaps,
        parity_report: dict[str, Any], holder_id: str,
        monitored_signal_id: int, monitored_symbol: str,
        monitored_tier: str,
    ) -> V40OneShotReport: ...


def _positive_decimal(value: Any) -> Decimal | None:
    if type(value) is bool:
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() and d > 0 else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def _safe_status(status: str) -> V40OneShotReport:
    return V40OneShotReport(status=status)


def validate_monitored_first_position(
    observation: V40SupervisedPilotObservation,
    caps: LiveCaps,
    permit: LiveMutationPermit,
) -> str:
    """Pure fail-closed validation; true flags are NOT independent proofs."""
    if type(observation) is not V40SupervisedPilotObservation:
        return "V40_HANDOFF_MONITOR_OBSERVATION_INVALID"
    if not (
        observation.status == FRESH_SIGNAL_STATUS
        and observation.source_status ==
            "V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING"
        and type(observation.signal_id) is int
        and observation.signal_id > 0
        and type(observation.signal_symbol) is str
        and observation.signal_symbol.endswith("_USDT")
        and observation.signal_symbol not in ("BTC_USDT", "ETH_USDT")
        and observation.signal_tier in ("LOW_100", "HIGH_225")
        and observation.fresh_signal_count == 1
        and observation.source_verified is True
        and observation.account_flat_verified is True
        and observation.real_signal_economics_observed is True
        and observation.candidate_public_contract_and_cross_1x_verified is True
        and observation.runtime_journal_status == FIRST_JOURNAL_STATUS
        and observation.first_position_journal_clean is True
    ):
        return "V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"
    # These connections are intentionally FALSE in the currently deployed
    # monitor. Never infer their truth from a passed journal or MEXC GET.
    if not (
        observation.enabled_position_capacity == 1
        and observation.live_adapter_connected is True
        and observation.native_stop_management_connected is True
        and observation.continuous_nonflat_guardian_connected is True
    ):
        return "V40_HANDOFF_NATIVE_STOP_GUARDIAN_OR_ADAPTER_NOT_CONNECTED"
    if not (
        observation.entry_allowed is False
        and observation.live_authorization is False
        and observation.mexc_orders_sent == 0
        and observation.database_writes == 0
    ):
        return "V40_HANDOFF_MONITOR_MUTATION_OR_AUTHORITY_VIOLATION"
    if type(caps) is not LiveCaps:
        return "V40_HANDOFF_PILOT_CAPS_INVALID"
    equity = _positive_decimal(observation.account_equity_usdt)
    if equity is None:
        return "V40_HANDOFF_EQUITY_UNVERIFIED"
    if _positive_decimal(observation.theoretical_notional_usdt) is None:
        return "V40_HANDOFF_STRATEGY_SIZE_UNVERIFIED"
    budget = min(PILOT_MAX_NOTIONAL_USDT, equity * PILOT_MAX_EQUITY_FRACTION)
    if (
        caps.leverage != 1 or caps.margin_type != 2
        or caps.max_order_usdt > budget
        or caps.max_portfolio_usdt > budget
        or permit.max_position_notional_usdt > budget
        or permit.max_total_notional_usdt > budget
        or permit.max_position_notional_usdt > caps.max_order_usdt
        or permit.max_total_notional_usdt > caps.max_portfolio_usdt
        or permit.leverage != 1 or permit.scope != "T100_TINY_PILOT"
    ):
        return "V40_HANDOFF_PILOT_BUDGET_OR_CROSS_LIMIT_EXCEEDED"
    return "V40_HANDOFF_READY_TO_RECHECK_EXACT_SOURCE_NONAUTHORIZING"


async def run_monitored_real_signal_once(
    *, observation: V40SupervisedPilotObservation,
    coordinator: MonitoredOneShotExecutor,
    permit: LiveMutationPermit,
    caps: LiveCaps,
    parity_report: dict[str, Any],
    holder_id: str,
) -> V40OneShotReport:
    """One externally authorized attempt; NEVER loop, force flags or retry.

    Even when this guard passes, the real coordinator must independently
    fetch the same fresh signal, acquire its fence, recalculate all portfolio
    budgets and contract terms, quote exact depth, claim at most ONE POST,
    reconcile native MARKET stop, commit fills and verify post-commit recovery.
    A possibly durable mutation or an exception always requires investigation.
    """
    # Do not even read the mutable monitor object or call any collaborator
    # until the independently issued live permit AND adapter are certified.
    require_certified_transport_permit(permit)
    state = validate_monitored_first_position(observation, caps, permit)
    if state != "V40_HANDOFF_READY_TO_RECHECK_EXACT_SOURCE_NONAUTHORIZING":
        return _safe_status(state)
    if not isinstance(parity_report, dict) or not holder_id:
        return _safe_status("V40_HANDOFF_PARITY_OR_HOLDER_MISSING")
    try:
        result = await coordinator.run_one(
            permit=permit, caps=caps, parity_report=parity_report,
            holder_id=holder_id,
            monitored_signal_id=observation.signal_id,
            monitored_symbol=observation.signal_symbol,
            monitored_tier=observation.signal_tier,
        )
        if type(result) is not V40OneShotReport:
            return V40OneShotReport(
                "V40_HANDOFF_COORDINATOR_OUTCOME_UNKNOWN_MANUAL_RECOVERY",
                claim_may_exist=True,
            )
        return result
    except Exception:
        # The exception might occur AFTER a durable reservation or MEXC POST.
        # Never release the claim or automatically run the candidate again.
        return V40OneShotReport(
            "V40_HANDOFF_COORDINATOR_EXCEPTION_MANUAL_RECOVERY",
            claim_may_exist=True,
        )
