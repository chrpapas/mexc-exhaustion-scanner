from __future__ import annotations

from typing import Any

from app.daily_regime import DAILY_REGIME_REQUIRED_FEATURES, daily_regime_state

# Frozen Continuation Core V1, discovered 1 Sep 2026.
CONTINUATION_CORE_V1_VERSION = "continuation_core_v1"
CONTINUATION_CORE_V1_RUN_SCORE_MIN = 5.0
CONTINUATION_CORE_V1_EMA_DISTANCE_ATR_MIN = 3.0
CONTINUATION_CORE_V1_CROSS_SECTION_MIN = 0.99
CONTINUATION_CORE_V1_PREVIOUS_MOMENTUM_MIN = 0.0

# Frozen Daily-Confirmed Core V1, discovered 1 Sep 2026 23:25 CEST.
DAILY_CONFIRMED_CORE_V1_VERSION = "daily_confirmed_core_v1"
DAILY_CORE_SKIP_STRATEGY = "tp5_sl75_daily_core_skip_v1"

CORE_REQUIRED_FEATURES = (
    "run_score",
    "distance_above_ema20_atr_4h",
    "previous_momentum_1h",
    "cross_section_percentile",
)
DAILY_CONFIRMED_CORE_REQUIRED_FEATURES = CORE_REQUIRED_FEATURES + DAILY_REGIME_REQUIRED_FEATURES


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def continuation_core_v1_missing_features(features: dict[str, Any]) -> tuple[str, ...]:
    missing: list[str] = []
    for key in CORE_REQUIRED_FEATURES:
        if _number(features.get(key)) is None:
            missing.append(key)
    return tuple(missing)


def continuation_core_v1_state(features: dict[str, Any]) -> bool | None:
    if continuation_core_v1_missing_features(features):
        return None
    run_score = float(features["run_score"])
    ema_distance = float(features["distance_above_ema20_atr_4h"])
    previous_momentum = float(features["previous_momentum_1h"])
    cross_section = float(features["cross_section_percentile"])
    return (
        run_score >= CONTINUATION_CORE_V1_RUN_SCORE_MIN
        and ema_distance >= CONTINUATION_CORE_V1_EMA_DISTANCE_ATR_MIN
        and (
            previous_momentum > CONTINUATION_CORE_V1_PREVIOUS_MOMENTUM_MIN
            or cross_section >= CONTINUATION_CORE_V1_CROSS_SECTION_MIN
        )
    )


def daily_confirmed_core_v1_missing_features(features: dict[str, Any]) -> tuple[str, ...]:
    missing = list(continuation_core_v1_missing_features(features))
    for key in DAILY_REGIME_REQUIRED_FEATURES:
        value = features.get(key)
        if key == "daily_close_above_ema20":
            if not isinstance(value, bool):
                missing.append(key)
        elif _number(value) is None:
            missing.append(key)
    return tuple(dict.fromkeys(missing))


def daily_confirmed_core_v1_state(features: dict[str, Any]) -> bool | None:
    core = continuation_core_v1_state(features)
    daily = daily_regime_state(features)
    if core is None or daily is None:
        return None
    return core and daily


def daily_confirmed_core_v1_snapshot_metadata(features: dict[str, Any]) -> dict[str, Any]:
    missing = daily_confirmed_core_v1_missing_features(features)
    state = daily_confirmed_core_v1_state(features)
    return {
        "continuation_core_v1_version": CONTINUATION_CORE_V1_VERSION,
        "continuation_core_v1_computable": continuation_core_v1_state(features) is not None,
        "continuation_core_v1_flagged": continuation_core_v1_state(features),
        "daily_confirmed_core_v1_version": DAILY_CONFIRMED_CORE_V1_VERSION,
        "daily_confirmed_core_v1_computable": state is not None,
        "daily_confirmed_core_v1_flagged": state,
        "daily_confirmed_core_v1_missing_fields": list(missing),
        "daily_core_live_admission_strategy": DAILY_CORE_SKIP_STRATEGY,
    }
