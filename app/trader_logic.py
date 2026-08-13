from __future__ import annotations

import math


def short_return_pct(entry_price: float, current_price: float) -> float:
    return (entry_price - current_price) / entry_price * 100.0


def ratchet_profit_floor(
    *,
    peak_profit_pct: float,
    activation_pct: float,
    step_pct: float,
) -> float | None:
    if peak_profit_pct < activation_pct:
        return None
    steps = math.floor((peak_profit_pct - activation_pct) / step_pct + 1e-12)
    return activation_pct + steps * step_pct


def trailing_profit_floor(
    *,
    peak_profit_pct: float,
    activation_pct: float,
    giveback_pct: float,
) -> float | None:
    if peak_profit_pct < activation_pct:
        return None
    return max(activation_pct, peak_profit_pct - giveback_pct)


def next_profit_floor(
    *,
    exit_strategy: str,
    peak_profit_pct: float,
    activation_pct: float,
    step_pct: float,
    giveback_pct: float,
) -> float | None:
    if exit_strategy == "ratchet_5":
        return ratchet_profit_floor(
            peak_profit_pct=peak_profit_pct,
            activation_pct=activation_pct,
            step_pct=step_pct,
        )
    if exit_strategy == "trailing_5":
        return trailing_profit_floor(
            peak_profit_pct=peak_profit_pct,
            activation_pct=activation_pct,
            giveback_pct=giveback_pct,
        )
    raise ValueError(f"unsupported exit strategy {exit_strategy}")

def maturity_exit_reason(
    *,
    position_maturity: str,
    current_return_pct: float,
    age_seconds: float,
    profit_target_pct: float,
) -> str | None:
    if position_maturity == "profit_20":
        return "profit_target_20" if current_return_pct >= profit_target_pct else None
    horizons = {"1d": 86400, "2d": 172800, "3d": 259200, "7d": 604800}
    if position_maturity not in horizons:
        raise ValueError(f"unsupported position maturity {position_maturity}")
    return f"maturity_{position_maturity}" if age_seconds >= horizons[position_maturity] else None

