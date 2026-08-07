from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    RunFeatures,
    RunThresholds,
    score_exhaustion,
    score_run,
)


def full_features() -> RunFeatures:
    return RunFeatures(
        return_24h=0.35,
        return_72h=0.60,
        btc_return_24h=0.02,
        residual_return_24h=0.33,
        cross_section_percentile=0.99,
        volume_zscore_15m=4.2,
        distance_above_ema20_atr_4h=3.8,
        amount_24h=50_000_000,
        spread_pct=0.08,
        funding_rate=0.001,
        fair_index_premium_pct=0.4,
        hold_vol=1_000_000,
    )


def test_full_candidate_scores_six() -> None:
    score, reasons, required_ok = score_run(full_features(), RunThresholds())
    assert required_ok
    assert score == 6
    assert len(reasons) == 6


def test_illiquid_contract_is_rejected() -> None:
    original = full_features()
    features = RunFeatures(**{**original.as_dict(), "amount_24h": 1_000_000})
    score, reasons, required_ok = score_run(features, RunThresholds())
    assert not required_ok
    assert score == 0
    assert reasons == []


def test_exhaustion_score_detects_reversal_structure() -> None:
    features = ExhaustionFeatures(
        upper_wick_ratio_15m=0.50,
        close_location_15m=0.20,
        momentum_1h=-0.01,
        previous_momentum_1h=0.08,
        momentum_decelerating=True,
        below_ema9_15m=True,
        lower_high_and_close=True,
        structural_break_15m=True,
        volume_zscore_15m=2.0,
    )
    score, reasons = score_exhaustion(features, ExhaustionThresholds())
    assert score == 7
    assert "15m structural break" in reasons
