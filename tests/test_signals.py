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


def neutral_exhaustion(**overrides) -> ExhaustionFeatures:
    values = {
        "upper_wick_ratio_15m": 0.10,
        "close_location_15m": 0.70,
        "momentum_1h": 0.01,
        "previous_momentum_1h": 0.02,
        "momentum_decelerating": False,
        "below_ema9_15m": False,
        "lower_high_and_close": False,
        "structural_break_15m": False,
        "volume_zscore_15m": 0.5,
    }
    values.update(overrides)
    return ExhaustionFeatures(**values)


def state_features(return_24h: float, return_72h: float) -> RunFeatures:
    return RunFeatures(
        return_24h=return_24h,
        return_72h=return_72h,
        btc_return_24h=0.0013,
        residual_return_24h=return_24h - 0.0013,
        cross_section_percentile=0.95,
        volume_zscore_15m=2.0,
        distance_above_ema20_atr_4h=2.0,
        amount_24h=20_000_000,
        spread_pct=0.10,
        funding_rate=0.0,
        fair_index_premium_pct=0.0,
        hold_vol=1_000_000,
    )


def test_hei_like_runner_is_run_watch() -> None:
    from app.signals import MarketStateThresholds, classify_market_state

    level, _ = classify_market_state(
        state_features(0.112, 1.317),
        run_score=3,
        exhaustion_features=neutral_exhaustion(),
        exhaustion_score=0,
        thresholds=MarketStateThresholds(),
    )
    assert level == "run_watch"


def test_cys_like_fading_runner_is_exhaustion_watch() -> None:
    from app.signals import MarketStateThresholds, classify_market_state

    level, reasons = classify_market_state(
        state_features(0.0199, 0.6252),
        run_score=4,
        exhaustion_features=neutral_exhaustion(),
        exhaustion_score=0,
        thresholds=MarketStateThresholds(),
    )
    assert level == "exhaustion_watch"
    assert any("cooled" in reason for reason in reasons)


def test_bico_like_fading_runner_is_exhaustion_watch() -> None:
    from app.signals import MarketStateThresholds, classify_market_state

    level, _ = classify_market_state(
        state_features(-0.0136, 1.8092),
        run_score=3,
        exhaustion_features=neutral_exhaustion(),
        exhaustion_score=1,
        thresholds=MarketStateThresholds(),
    )
    assert level == "exhaustion_watch"


def test_active_pump_can_flip_to_exhaustion_before_24h_cools() -> None:
    from app.signals import MarketStateThresholds, classify_market_state

    exhaustion = neutral_exhaustion(
        momentum_decelerating=True,
        below_ema9_15m=True,
        lower_high_and_close=True,
    )
    level, _ = classify_market_state(
        state_features(0.25, 0.70),
        run_score=5,
        exhaustion_features=exhaustion,
        exhaustion_score=3,
        thresholds=MarketStateThresholds(),
    )
    assert level == "exhaustion_watch"


def test_short_gate_requires_exhaustion_state_and_structural_break() -> None:
    from app.signals import MarketStateThresholds, classify_market_state

    exhaustion = neutral_exhaustion(
        momentum_decelerating=True,
        below_ema9_15m=True,
        lower_high_and_close=True,
        structural_break_15m=True,
        volume_zscore_15m=2.0,
    )
    score, _ = score_exhaustion(exhaustion, ExhaustionThresholds())
    level, _ = classify_market_state(
        state_features(0.02, 0.80),
        run_score=4,
        exhaustion_features=exhaustion,
        exhaustion_score=score,
        thresholds=MarketStateThresholds(),
    )
    assert level == "exhaustion_watch"
    assert exhaustion.structural_break_15m
    assert score >= 3
