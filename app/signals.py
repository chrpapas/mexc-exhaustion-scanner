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
class RunThresholds:
    min_amount_24h: float = 10_000_000
    max_spread_pct: float = 0.25
    min_return_24h: float = 0.20
    min_return_72h: float = 0.30
    min_residual_return_24h: float = 0.15
    min_cross_section_percentile: float = 0.95
    min_volume_zscore_15m: float = 2.5
    min_distance_ema_atr_4h: float = 2.5


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
