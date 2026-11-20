"""One-shot first real-money V40 entry parity is not autonomous certification."""
import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_authorization import V40_CANARY_SCOPE
from app.t100_live_execution import LiveCaps
from app.t100_live_strategy_parity_gate import (
    FROZEN_V40_ARCHIVE_SHA256,
    assess_strategy_parity_report,
    assess_v40_first_canary_entry_parity_report,
)
import app.t100_live_v40_entry_coordinator as coord


def frozen_entry_parity():
    return dict(
        status="V40_ENTRY_FILTER_AND_REFERENCE_CAPACITY_PARITY_PASS",
        archive_sha256=FROZEN_V40_ARCHIVE_SHA256,
        candidates=146,
        prior_candles=7154,
        reference_entry_notionals_exact=648,
        discrepancies=0,
    )


def test_restricted_first_canary_accepts_exact_frozen_entry_parity_only():
    report=frozen_entry_parity()
    full=assess_strategy_parity_report(report)
    assert full["strategy_parity_verified"] is False
    partial=assess_v40_first_canary_entry_parity_report(report)
    assert partial["first_canary_entry_parity_verified"] is True
    assert partial["full_autonomous_mode_certified"] is False
    assert partial["live_authorization"] is False
    assert partial["entry_allowed"] is False


@pytest.mark.parametrize("change",[
    {"archive_sha256":"unreviewed"},
    {"candidates":145},
    {"prior_candles":7153},
    {"reference_entry_notionals_exact":647},
    {"discrepancies":1},
    {"candidates":True},
    {"prior_candles":7154.0},
    {"status":"UNVERIFIED"},
])
def test_frozen_entry_mismatch_denies_even_single_canary(change):
    report=frozen_entry_parity()
    report.update(change)
    checked=assess_v40_first_canary_entry_parity_report(report)
    assert checked["first_canary_entry_parity_verified"] is False


def test_coordinator_rejects_invalid_canary_entry_parity_before_io(monkeypatch):
    monkeypatch.setattr(
        coord,"require_certified_transport_permit",lambda p:p,
    )
    # __new__ avoids constructing unused production DB/venue collaborators.
    one=object.__new__(coord.V40OneShotCoordinator)
    one.initial_pilot_only=True
    class Source:
        async def latest_candidates(self,*,observed_at):
            raise AssertionError("source read must be after frozen parity")
    one.source=Source()
    p=SimpleNamespace(
        scope=V40_CANARY_SCOPE,
        signal_id=42,symbol="SOL_USDT",
    )
    caps=LiveCaps(
        max_order_usdt=D("12.50"),
        max_portfolio_usdt=D("12.50"),
        max_daily_loss_usdt=D("12.50"),
    )
    with pytest.raises(coord.V40CoordinatorHalted,match="ENTRY_PARITY"):
        asyncio.run(one.run_one(
            permit=p,caps=caps,parity_report={"status":"UNVERIFIED"},
            holder_id="offline-test",
            monitored_signal_id=42,
            monitored_symbol="SOL_USDT",
            monitored_tier="LOW_100",
        ))
