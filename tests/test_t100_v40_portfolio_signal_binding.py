"""No-I/O regression tests for frozen Stage2 four-slot identity handoff."""
from datetime import datetime, timezone

import pytest

from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_stage2_source import V40SourceBatch, V40SourceCandidate
from app.t100_v40_portfolio_signal_binding import (
    V40ReadOnlySignalIdentity,
    V40SignalBindingHalted,
    bind_first_unheld_stage2_identity,
)


def candidate(signal_id=592, symbol="HUMA_USDT", age=300.0, vol=18.445):
    return V40SourceCandidate(
        signal_id=signal_id, strategy_id="T100",
        symbol=symbol, admission_open_ts=1795102200,
        feature_open_ts=1795100400,
        entry_reference_price=0.1, stage1_atr7_pct=5.13,
        stage1_return_24h=31.66, physical_episode_seq=1,
        tier="LOW_100", prior_bars=(),
        pre24_vol_pct=vol, observed_age_seconds=age,
    )


def source(*signals):
    return V40SourceBatch(
        status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=datetime.now(timezone.utc),
        candidates=tuple(signals),
    )


def flat():
    return FourSlotSnapshotResult(status="FLAT_OBSERVED_NONAUTHORIZING")


def test_exact_genuine_candidate_identity_is_bound_but_never_authorized():
    bound=bind_first_unheld_stage2_identity(source(candidate()),flat())
    assert type(bound) is V40ReadOnlySignalIdentity
    assert (bound.signal_id,bound.symbol,bound.tier)==(
        592,"HUMA_USDT","LOW_100",
    )
    assert bound.vol30_passed is True
    assert bound.entry_allowed is False
    assert bound.live_authorization is False


def test_empty_source_has_no_binding():
    assert bind_first_unheld_stage2_identity(source(),flat()) is None


def test_first_candidate_with_vol30_skip_is_not_silently_replaced():
    first=candidate(vol=40.0)
    later=candidate(signal_id=593,symbol="OTHER_USDT",vol=2.0)
    # Both are in the same frozen admission bar: ID controls priority.
    bound=bind_first_unheld_stage2_identity(source(first,later),flat())
    assert bound.signal_id==592
    assert bound.vol30_passed is False


@pytest.mark.parametrize("age",[-1.0,901.0,float("nan")])
def test_invalid_or_stale_signal_cannot_be_bound(age):
    with pytest.raises(V40SignalBindingHalted):
        bind_first_unheld_stage2_identity(source(candidate(age=age)),flat())


def test_source_priority_must_be_frozen_order():
    first=candidate()
    later=candidate(signal_id=593,symbol="OTHER_USDT")
    with pytest.raises(V40SignalBindingHalted):
        bind_first_unheld_stage2_identity(source(later,first),flat())


def test_duplicate_signal_id_is_rejected():
    dup=candidate(signal_id=592,symbol="OTHER_USDT")
    with pytest.raises(V40SignalBindingHalted):
        bind_first_unheld_stage2_identity(source(candidate(),dup),flat())


def test_unproven_flat_account_is_rejected():
    with pytest.raises(V40SignalBindingHalted):
        bind_first_unheld_stage2_identity(
            source(candidate()),
            FourSlotSnapshotResult(status="UNKNOWN_ACCOUNT"),
        )
