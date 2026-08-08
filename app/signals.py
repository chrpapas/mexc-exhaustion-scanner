from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from app.models import Candle


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


@dataclass(frozen=True, slots=True)
class MarketStateThresholds:
    min_run_score: int = 3
    run_watch_min_24h: float = 0.08
    run_watch_min_72h: float = 0.20
    exhaustion_watch_min_72h: float = 0.30
    exhaustion_watch_min_24h: float = -0.25
    exhaustion_watch_max_24h: float = 0.08
    active_exhaustion_min_score: int = 2


@dataclass(frozen=True, slots=True)
class ExecutionRisk:
    tier: str
    execution_eligible: bool
    warning: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetestResult:
    confirmed: bool
    expired: bool
    invalidated: bool
    retest_at: datetime | None = None
    retest_high: float | None = None
    retest_close: float | None = None
    reason: str | None = None


def classify_execution_risk(
    features: RunFeatures,
    thresholds: RunThresholds,
    *,
    high_risk_min_amount_24h: float = 500_000,
    high_risk_max_spread_pct: float = 1.0,
) -> ExecutionRisk:
    spread = features.spread_pct
    standard = (
        features.amount_24h >= thresholds.min_amount_24h
        and spread is not None
        and spread <= thresholds.max_spread_pct
    )
    if standard:
        return ExecutionRisk(
            tier="standard",
            execution_eligible=True,
            warning=None,
            reasons=(),
        )

    reasons: list[str] = []
    if features.amount_24h < thresholds.min_amount_24h:
        reasons.append(
            f"24h turnover below standard ${thresholds.min_amount_24h:,.0f}"
        )
    if spread is None:
        reasons.append("spread unavailable")
    elif spread > thresholds.max_spread_pct:
        reasons.append(
            f"spread above standard {thresholds.max_spread_pct:.2f}%"
        )

    high_risk = (
        features.amount_24h >= high_risk_min_amount_24h
        and spread is not None
        and spread <= high_risk_max_spread_pct
    )
    if high_risk:
        return ExecutionRisk(
            tier="high_risk",
            execution_eligible=False,
            warning="HIGH RISK / LOW LIQUIDITY — elevated slippage and squeeze risk",
            reasons=tuple(reasons),
        )

    if features.amount_24h < high_risk_min_amount_24h:
        reasons.append(
            f"24h turnover below high-risk floor ${high_risk_min_amount_24h:,.0f}"
        )
    if spread is not None and spread > high_risk_max_spread_pct:
        reasons.append(
            f"spread above high-risk ceiling {high_risk_max_spread_pct:.2f}%"
        )
    return ExecutionRisk(
        tier="extreme_risk",
        execution_eligible=False,
        warning="EXTREME EXECUTION RISK — analytics only; do not auto-trade",
        reasons=tuple(reasons),
    )


def score_run(features: RunFeatures, thresholds: RunThresholds) -> tuple[int, list[str], bool]:
    # Liquidity is intentionally NOT a discovery gate. The third return value
    # now means the feature set is scorable; execution quality is classified
    # separately by classify_execution_risk().
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


def classify_market_state(
    run_features: RunFeatures,
    run_score: int,
    exhaustion_features: ExhaustionFeatures,
    exhaustion_score: int,
    thresholds: MarketStateThresholds,
) -> tuple[str | None, list[str]]:
    """Classify a runner as advancing or fading before breakdown confirmation."""
    r24 = run_features.return_24h
    r72 = run_features.return_72h
    if r24 is None or r72 is None:
        return None, []

    prior_run = r72 >= thresholds.exhaustion_watch_min_72h
    # A coin discovered late can have lost its current 24h momentum score while
    # still being in a valid post-pump exhaustion phase. Allow strong exhaustion
    # evidence to keep such a prior runner visible instead of requiring 3/6 run
    # points at the exact moment we first start observing it.
    late_prior_runner = prior_run and exhaustion_score >= 2
    if run_score < thresholds.min_run_score and not late_prior_runner:
        return None, []
    in_cooling_band = (
        thresholds.exhaustion_watch_min_24h
        <= r24
        <= thresholds.exhaustion_watch_max_24h
    )
    if prior_run and in_cooling_band:
        return "exhaustion_watch", [
            f"72h prior run >= {thresholds.exhaustion_watch_min_72h:.0%}",
            (
                "24h return cooled into "
                f"{thresholds.exhaustion_watch_min_24h:.0%} to "
                f"{thresholds.exhaustion_watch_max_24h:.0%} band"
            ),
        ]

    active_reversal = (
        prior_run
        and r24 > thresholds.exhaustion_watch_max_24h
        and exhaustion_score >= thresholds.active_exhaustion_min_score
        and exhaustion_features.momentum_decelerating
        and (exhaustion_features.below_ema9_15m or exhaustion_features.lower_high_and_close)
    )
    if active_reversal:
        return "exhaustion_watch", [
            f"72h prior run >= {thresholds.exhaustion_watch_min_72h:.0%}",
            "24h run still positive but intraday reversal evidence is building",
        ]

    advancing = r24 > thresholds.run_watch_min_24h and (
        r72 >= thresholds.run_watch_min_72h or run_score >= 4
    )
    if advancing:
        return "run_watch", [
            f"24h return > {thresholds.run_watch_min_24h:.0%}",
            "run remains in advancing state",
        ]

    return None, []


def evaluate_failed_retest(
    candles: list[Candle],
    *,
    breakdown_at: datetime,
    broken_level: float,
    atr_15m: float,
    tolerance_atr: float,
    window_candles: int,
) -> RetestResult:
    """Evaluate completed candles after a breakdown for a failed retest.

    Confirmation requires a later candle to trade back close to the broken
    support from below, fail to close above it, and close bearish. A close back
    above the level invalidates the breakdown. If the configured number of
    completed candles passes without either event, the breakdown attempt expires.
    """
    if broken_level <= 0 or atr_15m <= 0 or tolerance_atr <= 0 or window_candles <= 0:
        return RetestResult(False, False, False)

    after_break = [candle for candle in candles if candle.open_time > breakdown_at]
    if not after_break:
        return RetestResult(False, False, False)

    tolerance = atr_15m * tolerance_atr
    examined = after_break[:window_candles]
    for candle in examined:
        if candle.close > broken_level:
            return RetestResult(
                confirmed=False,
                expired=False,
                invalidated=True,
                retest_at=candle.open_time,
                retest_high=candle.high,
                retest_close=candle.close,
                reason="15m candle closed back above broken support",
            )

        approached = candle.high >= broken_level - tolerance
        rejected = candle.close < broken_level
        bearish = candle.close < candle.open
        if approached and rejected and bearish:
            return RetestResult(
                confirmed=True,
                expired=False,
                invalidated=False,
                retest_at=candle.open_time,
                retest_high=candle.high,
                retest_close=candle.close,
                reason="retest approached broken support and rejected below it",
            )

    expired = len(after_break) >= window_candles
    return RetestResult(
        confirmed=False,
        expired=expired,
        invalidated=False,
        reason=(
            f"no failed retest within {window_candles} completed 15m candles"
            if expired
            else None
        ),
    )
