from __future__ import annotations


# Frozen Parabolic Continuation Risk (PCR) sizing overlay.
# Discovered/frozen 30 Aug 2026 from the PONS/HNT/CATE tail investigation.
# These constants are deliberately code-frozen rather than environment-tunable.
PCR_RETURN_24H_THRESHOLD = 0.30
PCR_EMA_DISTANCE_ATR_THRESHOLD = 3.0
PCR_FLAGGED_POSITION_FRACTION = 0.025
PCR_BASE_POSITION_FRACTION = 0.05


def _numeric_feature(features: dict[str, object], key: str) -> float | None:
    value = features.get(key)
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed


def parabolic_continuation_risk(features: dict[str, object]) -> bool:
    """Return the frozen PCR flag used by both research and live sizing.

    A confirmed signal is PCR-flagged only when its signal-time snapshot has
    24h return >= +30% AND 4h EMA20 extension >= 3 ATR. Missing/unparseable
    features are treated as unflagged, matching the frozen research rule.
    """
    return_24h = _numeric_feature(features, "return_24h")
    ema_distance = _numeric_feature(features, "distance_above_ema20_atr_4h")
    return (
        return_24h is not None
        and ema_distance is not None
        and return_24h >= PCR_RETURN_24H_THRESHOLD
        and ema_distance >= PCR_EMA_DISTANCE_ATR_THRESHOLD
    )


def pcr_position_fraction(features: dict[str, object]) -> float:
    return (
        PCR_FLAGGED_POSITION_FRACTION
        if parabolic_continuation_risk(features)
        else PCR_BASE_POSITION_FRACTION
    )


def short_return_pct(entry_price: float, current_price: float) -> float:
    return (entry_price - current_price) / entry_price * 100.0


def short_price_for_return(entry_price: float, return_pct: float) -> float:
    return entry_price * (1.0 - return_pct / 100.0)


def protected_profit_floor_pct(
    *,
    peak_profit_pct: float,
    hard_floor_pct: float = 20.0,
    arm_pct: float = 25.0,
    trail_callback_pct: float = 15.0,
) -> float | None:
    """Profit floor for a short after protection is armed.

    The trail is defined in *price* terms, matching MEXC: lowest_price * (1 + callback).
    The protected floor never falls below the hard floor.
    """
    if peak_profit_pct < arm_pct:
        return None
    callback = trail_callback_pct / 100.0
    lowest_fraction = max(0.0, 1.0 - peak_profit_pct / 100.0)
    stop_fraction = lowest_fraction * (1.0 + callback)
    trail_floor = (1.0 - stop_fraction) * 100.0
    return max(hard_floor_pct, trail_floor)


def newly_breached_thresholds(
    *, max_adverse_pct: float, already_breached: set[int], thresholds: tuple[int, ...] = (100, 200, 300, 400)
) -> list[int]:
    return [t for t in thresholds if max_adverse_pct >= t and t not in already_breached]



def maturity_seconds(position_maturity: str) -> int:
    horizons = {
        "1d": 86400, "2d": 172800, "3d": 259200, "4d": 345600,
        "5d": 432000, "6d": 518400, "7d": 604800, "10d": 864000, "14d": 1209600,
    }
    if position_maturity not in horizons:
        raise ValueError(f"unsupported position maturity {position_maturity}")
    return horizons[position_maturity]


def tier_strategy_exit_reason(
    *,
    exit_strategy: str,
    position_maturity: str,
    current_return_pct: float,
    age_seconds: float,
    profit_target_pct: float,
) -> str | None:
    """Exit rule for v1.3 tier-specific positions.

    STANDARD positions use a fixed time hold. HIGH_RISK positions take the full
    position at the profit target or time out, whichever occurs first.
    """
    timeout_seconds = maturity_seconds(position_maturity)
    if exit_strategy == "fixed_time_standard":
        return f"standard_maturity_{position_maturity}" if age_seconds >= timeout_seconds else None
    if exit_strategy == "tp20_or_timeout":
        if current_return_pct >= profit_target_pct:
            return f"high_risk_profit_target_{profit_target_pct:g}"
        if age_seconds >= timeout_seconds:
            return f"high_risk_timeout_{position_maturity}"
        return None
    raise ValueError(f"unsupported tier exit strategy {exit_strategy}")

# Legacy helpers retained for backward-compatible tests and persisted v1.1 positions/tools.
def ratchet_profit_floor(*, peak_profit_pct: float, activation_pct: float, step_pct: float) -> float | None:
    import math
    if peak_profit_pct < activation_pct:
        return None
    steps = math.floor((peak_profit_pct - activation_pct) / step_pct + 1e-12)
    return activation_pct + steps * step_pct


def trailing_profit_floor(*, peak_profit_pct: float, activation_pct: float, giveback_pct: float) -> float | None:
    if peak_profit_pct < activation_pct:
        return None
    return max(activation_pct, peak_profit_pct - giveback_pct)


def next_profit_floor(*, exit_strategy: str, peak_profit_pct: float, activation_pct: float, step_pct: float, giveback_pct: float) -> float | None:
    if exit_strategy == "ratchet_5":
        return ratchet_profit_floor(peak_profit_pct=peak_profit_pct, activation_pct=activation_pct, step_pct=step_pct)
    if exit_strategy == "trailing_5":
        return trailing_profit_floor(peak_profit_pct=peak_profit_pct, activation_pct=activation_pct, giveback_pct=giveback_pct)
    raise ValueError(f"unsupported exit strategy {exit_strategy}")


def maturity_exit_reason(*, position_maturity: str, current_return_pct: float, age_seconds: float, profit_target_pct: float) -> str | None:
    if position_maturity == "profit_20":
        return "profit_target_20" if current_return_pct >= profit_target_pct else None
    return f"maturity_{position_maturity}" if age_seconds >= maturity_seconds(position_maturity) else None
