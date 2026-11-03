"""Fail-closed one-shot T100 tiny-pilot entry coordinator.

This coordinator is intentionally NOT a scheduler. It can attempt at most one
already-admitted entry and requires all runtime/review evidence immediately
before delegating to LiveOrderGateway.

A successful send result is ACK-only. It is not a completed pilot; protected
entry reconciliation must subsequently verify fills, position, native TP/SL
and durable checkpoint state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

from app.t100_live_authorization import (
    LiveMutationPermit,
    require_live_mutation_permit,
)
from app.t100_live_execution import EntryIntent, LiveOrderGateway
from app.t100_v40_admission import V40Admission


class TinyPilotHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class TinyPilotMarketEvidence:
    equity_usdt: Decimal
    portfolio_notional_usdt: Decimal
    today_loss_usdt: Decimal
    observed_vwap: Decimal
    book_age_ms: int
    signal_age_seconds: float


@dataclass(frozen=True)
class TinyPilotSubmission:
    order_id: str
    external_oid: str
    symbol: str
    notional_usdt: Decimal
    modeled_strategy_notional_usdt: Decimal
    pilot_notional_reduced: bool
    protected_entry_reconciliation_required: bool = True
    live_authorization: bool = False


class TinyPilotCoordinator:
    def __init__(
        self,
        *,
        gateway: LiveOrderGateway,
        dynamic_readiness_supplier: Callable[
            [], Awaitable[dict[str, Any]]
        ],
    ) -> None:
        self.gateway = gateway
        self.dynamic_readiness_supplier = dynamic_readiness_supplier

    async def submit_one(
        self,
        *,
        intent: EntryIntent,
        admission: V40Admission,
        permit: LiveMutationPermit,
        code_revision_report: dict[str, Any],
        strategy_parity_report: dict[str, Any],
        ws_pre_pilot_report: dict[str, Any],
        market: TinyPilotMarketEvidence,
    ) -> TinyPilotSubmission:
        require_live_mutation_permit(permit)
        if not isinstance(intent, EntryIntent):
            raise TinyPilotHalted("PILOT_ENTRY_INTENT_INVALID")
        if not isinstance(admission, V40Admission):
            raise TinyPilotHalted("PILOT_V40_ADMISSION_INVALID")
        if not isinstance(market, TinyPilotMarketEvidence):
            raise TinyPilotHalted("PILOT_MARKET_EVIDENCE_INVALID")

        if (
            not isinstance(code_revision_report, dict)
            or code_revision_report.get("code_revision_verified") is not True
        ):
            raise TinyPilotHalted("PILOT_CODE_REVISION_UNVERIFIED")
        if (
            not isinstance(strategy_parity_report, dict)
            or strategy_parity_report.get("strategy_parity_verified")
            is not True
        ):
            raise TinyPilotHalted("PILOT_STRATEGY_PARITY_UNVERIFIED")
        if (
            not isinstance(ws_pre_pilot_report, dict)
            or ws_pre_pilot_report.get(
                "private_ws_pre_pilot_contract_verified"
            )
            is not True
        ):
            raise TinyPilotHalted("PILOT_PRIVATE_WS_CONTRACT_UNVERIFIED")

        if (
            admission.action != "ADMIT"
            or admission.symbol != intent.symbol
            or admission.modeled_entry_notional <= 0
            or intent.notional_usdt > admission.modeled_entry_notional
        ):
            raise TinyPilotHalted(
                "PILOT_INTENT_NOT_COVERED_BY_V40_ADMISSION"
            )

        # First pilot is intentionally restricted to a flat account. This
        # check is repeated by the gateway against the exchange immediately
        # before intent persistence / POST.
        if market.portfolio_notional_usdt != 0:
            raise TinyPilotHalted("PILOT_REQUIRES_ZERO_PORTFOLIO_NOTIONAL")

        try:
            dynamic = await self.dynamic_readiness_supplier()
        except Exception as exc:
            raise TinyPilotHalted(
                "PILOT_DYNAMIC_READINESS_FAILED"
            ) from exc
        if not isinstance(dynamic, dict):
            raise TinyPilotHalted("PILOT_DYNAMIC_READINESS_INVALID")
        if dynamic.get("dynamic_readiness_verified") is not True:
            raise TinyPilotHalted("PILOT_DYNAMIC_READINESS_BLOCKED")

        account = dynamic.get("account_reconciliation")
        if (
            not isinstance(account, dict)
            or account.get("flat_account_reconciled") is not True
            or account.get("account_reconciled") is not True
        ):
            raise TinyPilotHalted("PILOT_REQUIRES_FLAT_RECONCILED_ACCOUNT")

        # Defensive checks against a future readiness-bundle regression.
        for field in (
            "durable_schema_deployed_verified",
            "unresolved_mutations_absent",
            "current_inventory_completeness_verified",
            "account_reconciled",
        ):
            if dynamic.get(field) is not True:
                raise TinyPilotHalted(
                    f"PILOT_DYNAMIC_GATE_FALSE:{field}"
                )

        try:
            order_id = await self.gateway.send_one(
                intent,
                permit=permit,
                equity_usdt=market.equity_usdt,
                portfolio_notional=market.portfolio_notional_usdt,
                today_loss_usdt=market.today_loss_usdt,
                observed_vwap=market.observed_vwap,
                book_age_ms=market.book_age_ms,
                signal_age_seconds=market.signal_age_seconds,
            )
        except Exception as exc:
            raise TinyPilotHalted(
                "PILOT_ENTRY_GATEWAY_HALTED_OR_OUTCOME_UNKNOWN"
            ) from exc

        if not isinstance(order_id, str) or not order_id:
            raise TinyPilotHalted("PILOT_ORDER_ID_INVALID")

        return TinyPilotSubmission(
            order_id=order_id,
            external_oid=intent.external_oid,
            symbol=intent.symbol,
            notional_usdt=intent.notional_usdt,
            modeled_strategy_notional_usdt=(
                admission.modeled_entry_notional
            ),
            pilot_notional_reduced=(
                intent.notional_usdt
                < admission.modeled_entry_notional
            ),
        )
