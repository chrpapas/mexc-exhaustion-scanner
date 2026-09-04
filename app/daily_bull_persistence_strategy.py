from __future__ import annotations

from typing import Any

from app.daily_core_strategy import continuation_core_v1_state
from app.daily_regime import daily_regime_state

# First-Entry Trend Persistence V1. Designed after the 3 Sep 2026 USELESS/PONS
# review and originally frozen as a research-only challenger at 20:53 CEST.
# v1.3.52 makes it selectable/promoted for live trader/subscriber admission,
# while preserving the exact frozen thresholds from v1.3.51.
DAILY_BULL_PERSISTENCE_V1_VERSION = "daily_bull_persistence_v1"
DAILY_BULL_PERSISTENCE_V1_DAILY_DISTANCE_ATR_MIN = 4.5
DAILY_BULL_PERSISTENCE_V1_EMA20_SLOPE_MIN = 0.075
DAILY_BULL_PERSISTENCE_V1_RUN_TO_BREAKDOWN_HOURS_MAX = 6.0

DAILY_CORE_PERSISTENCE_SKIP_STRATEGY = "tp5_sl75_daily_core_persistence_skip_v1"

PERSISTENCE_REQUIRED_FEATURES = (
    "daily_distance_above_ema20_atr",
    "daily_ema20_slope",
    "hours_run_to_breakdown",
)


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def daily_bull_persistence_v1_missing_features(features: dict[str, Any]) -> tuple[str, ...]:
    """Return only inputs needed for the persistence branch that is actually reachable.

    Daily/Core computability is handled by the existing Daily-Core gate first. If the
    signal is not Daily Bull, or Continuation Core is already true, Persistence V1 is
    deterministically false and its extra inputs are not required.
    """
    daily = daily_regime_state(features)
    core = continuation_core_v1_state(features)
    if daily is None or core is None:
        # The Daily-Core gate owns these missing-data decisions.
        return ()
    if not daily or core:
        return ()
    return tuple(key for key in PERSISTENCE_REQUIRED_FEATURES if _number(features.get(key)) is None)


def daily_bull_persistence_v1_state(features: dict[str, Any]) -> bool | None:
    """Flag an admitted Daily-Bull/Core-false setup whose HTF trend is still extreme.

    Returns:
      False: the persistence veto does not apply.
      True:  hard-skip candidate.
      None:  the persistence branch applies but required signal-time inputs are missing.
    """
    daily = daily_regime_state(features)
    core = continuation_core_v1_state(features)
    if daily is None or core is None:
        return None
    if not daily or core:
        return False

    distance = _number(features.get("daily_distance_above_ema20_atr"))
    slope = _number(features.get("daily_ema20_slope"))
    run_hours = _number(features.get("hours_run_to_breakdown"))
    if distance is None or slope is None or run_hours is None:
        return None

    return (
        distance >= DAILY_BULL_PERSISTENCE_V1_DAILY_DISTANCE_ATR_MIN
        and slope >= DAILY_BULL_PERSISTENCE_V1_EMA20_SLOPE_MIN
        and run_hours <= DAILY_BULL_PERSISTENCE_V1_RUN_TO_BREAKDOWN_HOURS_MAX
    )


def daily_bull_persistence_v1_snapshot_metadata(features: dict[str, Any]) -> dict[str, Any]:
    state = daily_bull_persistence_v1_state(features)
    missing = daily_bull_persistence_v1_missing_features(features)
    return {
        "daily_bull_persistence_v1_version": DAILY_BULL_PERSISTENCE_V1_VERSION,
        "daily_bull_persistence_v1_computable": state is not None,
        "daily_bull_persistence_v1_flagged": state,
        "daily_bull_persistence_v1_missing_fields": list(missing),
    }
