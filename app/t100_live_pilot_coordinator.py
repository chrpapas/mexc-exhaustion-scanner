"""Retired one-slot prototype; submission is unconditionally disabled.

The old admission interface lacks symbol/time provenance and cannot certify
full four-slot execution. Retain the import surface for explicit rejection;
no readiness supplier, persistence or gateway is called by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

from app.t100_live_authorization import (
    LiveMutationPermit,
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
        # Retired prototype: its admission object lost symbol/time provenance,
        # and a one-position path cannot certify the full four-slot runtime.
        # Reject before any readiness supplier, persistence or gateway call.
        raise TinyPilotHalted("PILOT_COORDINATOR_RETIRED_FULL_V40_REQUIRED")
