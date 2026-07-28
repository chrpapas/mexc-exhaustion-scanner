from app.signals import RunFeatures, RunThresholds, score_run


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
