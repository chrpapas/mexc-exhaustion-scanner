from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RunFeatures:
    return_24h: float | None
    return_72h: float | None
    btc_return_24h: float | None
    residual_return_24h: float | None
    cross_section_percentile: float | None
    volume_zscore_15m: float | None
    distance_above_ema20_atr_4h: float | None
    amount_24h: float
    spread_pct: float | None
    funding_rate: float | None
    fair_index_premium_pct: float | None
    hold_vol: float | None

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExhaustionFeatures:
    upper_wick_ratio_15m: float | None
    close_location_15m: float | None
    momentum_1h: float | None
    previous_momentum_1h: float | None
    momentum_decelerating: bool
    below_ema9_15m: bool
    lower_high_and_close: bool
    structural_break_15m: bool
    volume_zscore_15m: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunThresholds:
    min_amount_24h: float = 3_000_000
    max_spread_pct: float = 0.35
    min_return_24h: float = 0.12
    min_return_72h: float = 0.20
    min_residual_return_24h: float = 0.10
    min_cross_section_percentile: float = 0.90
    min_volume_zscore_15m: float = 1.5
    min_distance_ema_atr_4h: float = 1.5


@dataclass(frozen=True, slots=True)
class ExhaustionThresholds:
    min_upper_wick_ratio: float = 0.35
    max_close_location: float = 0.45
    min_volume_zscore: float = 1.25


def score_run(features: RunFeatures, thresholds: RunThresholds) -> tuple[int, list[str], bool]:
    required_ok = (
        features.amount_24h >= thresholds.min_amount_24h
        and features.spread_pct is not None
        and features.spread_pct <= thresholds.max_spread_pct
    )
    if not required_ok:
        return 0, [], False

    checks: list[tuple[bool, str]] = [
        (
            features.return_24h is not None and features.return_24h >= thresholds.min_return_24h,
            f"24h return >= {thresholds.min_return_24h:.0%}",
        ),
        (
            features.return_72h is not None and features.return_72h >= thresholds.min_return_72h,
            f"72h return >= {thresholds.min_return_72h:.0%}",
        ),
        (
            features.residual_return_24h is not None
            and features.residual_return_24h >= thresholds.min_residual_return_24h,
            f"BTC residual >= {thresholds.min_residual_return_24h:.0%}",
        ),
        (
            features.cross_section_percentile is not None
            and features.cross_section_percentile >= thresholds.min_cross_section_percentile,
            f"cross-section percentile >= {thresholds.min_cross_section_percentile:.0%}",
        ),
        (
            features.volume_zscore_15m is not None
            and features.volume_zscore_15m >= thresholds.min_volume_zscore_15m,
            f"15m volume z-score >= {thresholds.min_volume_zscore_15m:g}",
        ),
        (
            features.distance_above_ema20_atr_4h is not None
            and features.distance_above_ema20_atr_4h >= thresholds.min_distance_ema_atr_4h,
            f"distance above 4h EMA20 >= {thresholds.min_distance_ema_atr_4h:g} ATR",
        ),
    ]
    reasons = [label for passed, label in checks if passed]
    return len(reasons), reasons, True


def score_exhaustion(
    features: ExhaustionFeatures,
    thresholds: ExhaustionThresholds,
) -> tuple[int, list[str]]:
    checks: list[tuple[bool, str]] = [
        (
            features.upper_wick_ratio_15m is not None
            and features.upper_wick_ratio_15m >= thresholds.min_upper_wick_ratio,
            f"15m upper wick >= {thresholds.min_upper_wick_ratio:.0%}",
        ),
        (
            features.close_location_15m is not None
            and features.close_location_15m <= thresholds.max_close_location,
            f"15m close in bottom {thresholds.max_close_location:.0%} of range",
        ),
        (features.momentum_decelerating, "1h momentum decelerating"),
        (features.below_ema9_15m, "15m close below EMA9"),
        (features.lower_high_and_close, "lower high + lower close"),
        (features.structural_break_15m, "15m structural break"),
        (
            features.volume_zscore_15m is not None
            and features.volume_zscore_15m >= thresholds.min_volume_zscore,
            f"15m volume z-score >= {thresholds.min_volume_zscore:g}",
        ),
    ]
    reasons = [label for passed, label in checks if passed]
    return len(reasons), reasons
