"""V40 full-sizing reference tests, never trade or use MEXC secrets."""
from datetime import datetime, timezone

import pytest

from app.t100_200_strategy import STRATEGY_ID
from app.t100_live_v40_stage2_source import V40SourceBatch, V40SourceCandidate
from app.t100_v40_admission import PriorBar
from app.t100_v40_unreleased_full_portfolio import (
    V40FullPortfolioHalted, project_frozen_v40_multi_slot,
)

BAR = 1800
ADMIT = 2_000 * BAR


def batch(n=5, *, atr=4.5, vol=0.0):
    bars = tuple(
        PriorBar(open_ts=ADMIT-(49-i)*BAR, high=100., close=100.,
                 amount_usdt=10_000_000.)
        for i in range(49)
    )
    signals = tuple(
        V40SourceCandidate(
            signal_id=100+i,strategy_id=STRATEGY_ID,
            symbol=f"ALT{i+1}_USDT",admission_open_ts=ADMIT,
            feature_open_ts=ADMIT-BAR,entry_reference_price=100.,
            stage1_atr7_pct=atr,stage1_return_24h=.25,
            physical_episode_seq=1,
            tier="HIGH_225" if atr>=5.7 else "LOW_100",
            prior_bars=bars,pre24_vol_pct=vol,observed_age_seconds=10.,
        )
        for i in range(n)
    )
    return V40SourceBatch(
        status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=datetime.now(timezone.utc),
        candidates=signals,
    )


def test_full_v40_four_slots_and_low_tier_25pct_target():
    result = project_frozen_v40_multi_slot(
        source=batch(), equity_usdt=100_000.,
        marked_existing_usdt=0., owned_open_symbols=(),
    )
    assert len(result.entries) == 4
    assert result.resulting_slot_count == 4
    assert result.entries[0].desired_notional_usdt == 25_000.
    assert result.skipped == ((104, "four_slot_limit"),)
    assert result.exchange_posts == 0
    assert result.live_authorization is False


def test_high_tier_desired_target_is_62point5pct_before_clipping():
    result = project_frozen_v40_multi_slot(
        source=batch(1, atr=5.8), equity_usdt=100_000.,
        marked_existing_usdt=0., owned_open_symbols=(),
    )
    assert result.entries[0].desired_notional_usdt == 62_500.
    assert result.entries[0].tier == "HIGH_225"


def test_same_symbol_is_not_admitted_twice():
    result = project_frozen_v40_multi_slot(
        source=batch(1), equity_usdt=100_000.,
        marked_existing_usdt=0., owned_open_symbols=("ALT1_USDT",),
    )
    assert len(result.entries) == 0
    assert result.skipped == ((100, "duplicate_symbol"),)


def test_vol30_history_mismatch_cannot_forge_a_pass():
    with pytest.raises(V40FullPortfolioHalted, match="VOL30_DRIFT"):
        project_frozen_v40_multi_slot(
            source=batch(1, vol=3.), equity_usdt=100_000.,
            marked_existing_usdt=0., owned_open_symbols=(),
        )


def test_full_account_projection_never_allows_real_orders():
    projection = project_frozen_v40_multi_slot(
        source=batch(1), equity_usdt=100_000.,
        marked_existing_usdt=0., owned_open_symbols=(),
    )
    assert projection.risk_leverage_approved is False
    assert projection.native_exchange_execution_verified is False
