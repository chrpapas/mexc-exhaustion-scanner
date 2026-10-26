"""Explicit sizing boundary between V40 research parity and a tiny live pilot.

A supervised pilot can intentionally use less notional than the frozen V40
model in order to validate execution mechanics with limited real-money risk.
That downsizing must never be reported as historical strategy parity.

This module has no exchange or database calls and never authorizes an order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_v40_admission import V40Admission


class PilotSizingHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class PilotSizingPlan:
    modeled_v40_notional_usdt: Decimal
    pilot_notional_usdt: Decimal
    pilot_cap_usdt: Decimal
    equity_usdt: Decimal
    historical_notional_parity: bool
    purpose: str
    live_authorization: bool = False


def _positive(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PilotSizingHalted(f"INVALID_{field}") from exc
    if not value.is_finite() or value <= 0:
        raise PilotSizingHalted(f"INVALID_{field}")
    return value


def plan_tiny_pilot_notional(
    admission: V40Admission,
    *,
    equity_usdt: Any,
    explicit_pilot_cap_usdt: Any,
    max_equity_fraction: Decimal = Decimal("0.25"),
) -> PilotSizingPlan:
    """Return a deliberately bounded pilot notional.

    The default 25% equity ceiling is a pilot-risk boundary, not a V40
    strategy parameter. Callers may choose an even smaller explicit cap.
    The result reports historical_notional_parity only when no pilot cap or
    equity-fraction cap changes the V40 modeled notional.
    """
    if not isinstance(admission, V40Admission):
        raise PilotSizingHalted("INVALID_V40_ADMISSION")
    if admission.action != "ADMIT":
        raise PilotSizingHalted(
            f"V40_NOT_ADMITTED:{admission.action}:{admission.reason}"
        )
    equity = _positive(equity_usdt, "EQUITY")
    cap = _positive(explicit_pilot_cap_usdt, "PILOT_CAP")
    if (
        not isinstance(max_equity_fraction, Decimal)
        or not max_equity_fraction.is_finite()
        or max_equity_fraction <= 0
        or max_equity_fraction > Decimal("1")
    ):
        raise PilotSizingHalted("INVALID_PILOT_EQUITY_FRACTION")

    modeled = _positive(
        admission.modeled_notional_usdt,
        "MODELED_V40_NOTIONAL",
    )
    equity_ceiling = equity * max_equity_fraction
    pilot = min(modeled, cap, equity_ceiling)
    if pilot <= 0:
        raise PilotSizingHalted("PILOT_NOTIONAL_ZERO")

    parity = pilot == modeled
    return PilotSizingPlan(
        modeled_v40_notional_usdt=modeled,
        pilot_notional_usdt=pilot,
        pilot_cap_usdt=cap,
        equity_usdt=equity,
        historical_notional_parity=parity,
        purpose=(
            "V40_NOTIONAL_PARITY"
            if parity
            else "EXECUTION_PILOT_DOWNSIZED_NONPARITY"
        ),
    )
