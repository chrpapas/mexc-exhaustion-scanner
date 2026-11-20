"""Release gate: unreleased trailing cycle must never reach DB/MEXC."""
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_mutation_executor import StopMutationExecutor
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_live_v40_trailing_restart import V40TrailingRestartView
from app.t100_v40_unreleased_exit_policy import V40ExitDecision
from app.t100_v40_unreleased_native_stop_cycle import (
    V40UnreleasedStopCycleHalted, submit_one_verified_trailing_amendment,
)


class UnsafeSpy:
    async def submit_native_short_stop_tightening(self, **kwargs):
        raise AssertionError("MEXC POST MUST NEVER BE REACHED")


class JournalSpy:
    async def prepare(self, *_):
        raise AssertionError("DATABASE WRITE MUST NEVER BE REACHED")


@pytest.mark.asyncio
async def test_current_native_stop_gate_blocks_before_any_database_or_mexc_post():
    cp = PositionCheckpoint(
        symbol="EXAMPLE_USDT",external_oid="source",exchange_order_id="91",
        position_id="52",contracts=D("1"),entry_price=D("100"),
        stop_order_id="92",stop_price=D("175"),best_profit_pct=D("0"),
        armed=False,lifecycle_state="OPEN",checkpoint_version=1,
    )
    restart = V40TrailingRestartView(
        status="V40_TRAILING_RESTART_DURABLE_FLOOR_ONLY_FREEZE",
        position_id="52",symbol="EXAMPLE_USDT",checkpoint_version=1,
        recovered_best_profit_pct=D("0"),recovered_armed=False,
        recovered_stop_price=D("175"),next_best_profit_pct=D("0"),
        next_armed=False,proposed_stop_price=D("175"),
        proposed_stop_tightening=False,durable_revision_history_verified=False,
    )
    lifecycle = StopAmendmentLifecycleCoordinator(JournalSpy())
    executor = StopMutationExecutor(lifecycle=lifecycle,transport=UnsafeSpy())
    with pytest.raises(
        V40UnreleasedStopCycleHalted, match="EXCHANGE_CONTRACT_NOT_CERTIFIED"
    ):
        await submit_one_verified_trailing_amendment(
            signal_id=5,previous=cp,
            planned=V40ExitDecision("NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"),
            preflight=TrailingProposalReview("TRAILING_INPUT_OR_TIMESTAMP_INVALID"),
            restart=restart,permit=None,
            lifecycle=lifecycle,mutation=executor,
        )
