"""V40-specific cross margin stress envelope for frozen short SL75.

PURE / OFFLINE. This deliberately wraps the generic cross stress model so
its 20% default shock is NOT misrepresented as a V40 SL75 stress test.

The scenario must reach at least 175% of each entry fair reference plus a
separate adverse fair-price overshoot. This is NOT the venue's liquidation
model, order fill price, stop-trigger execution path, a guarantee of the
overshoot's severity, or an authorization for live execution.

Requires the generic cross-margin engine's own independent inventory,
risk-tier, cross-mode and fair-price evidence gates. It never sets these
flags, and its result never certifies liquidation/entry/live authorization.
Do not connect it to an order-gateway or promote hypothetical inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from app.t100_live_cross_margin_stress import (
    CrossAccount, ShortCrossPosition, assess_cross_margin_shock,
)

D = Decimal
_STOP_MULTIPLIER = D("1.75")  # frozen 75% adverse SHORT stop reference
_MIN_OVERSHOOT_CURRENT = D("0.05")  # scenario floor only; not a guarantee
_MAX_MODEL_SHOCK = D("2")


@dataclass(frozen=True)
class V40CrossStressEnvelope:
    status: str
    scenario_adverse_fair_pct: D | None = None
    required_adverse_fair_pct: D | None = None
    required_overshoot_pct: D = D("5")
    scenario_stress_survived: bool = False
    stressed_tiers_certified: bool = False
    account_collateral_certified: bool = False
    native_stop_protection_verified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


def _nonnegative(raw: Any) -> D:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        x = D(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_NUMBER") from None
    if not x.is_finite() or x < 0:
        raise ValueError("INVALID_NUMBER")
    return x


def assess_v40_cross_stop_envelope(
    *, account: CrossAccount, positions: Sequence[ShortCrossPosition],
    adverse_fair_price_pct: Any = None,
    additional_gap_pct_of_current: Any = _MIN_OVERSHOOT_CURRENT,
) -> V40CrossStressEnvelope:
    """Check a hypothetical simultaneous upward shock reaches SL75 plus gap.

    Required shock is measured relative to CURRENT fair price, while the
    SL75 reference derives from ENTRY fair price. This avoids the unsafe
    assumption that price is always at entry. No path/partial liquidation
    model is supplied, even when the static stress scenario survives.
    """
    def blocked(reason: str, **flags: Any) -> V40CrossStressEnvelope:
        return V40CrossStressEnvelope(status=reason, **flags)

    if (
        not isinstance(account, CrossAccount)
        or not isinstance(positions, (tuple, list))
        or not 1 <= len(positions) <= 4
        or any(type(p) is not ShortCrossPosition for p in positions)
    ):
        return blocked("V40_CROSS_STRESS_INPUT_INVALID")

    try:
        account.validate()
        gap = _nonnegative(additional_gap_pct_of_current)
        if gap < _MIN_OVERSHOOT_CURRENT or gap > D("1"):
            return blocked("V40_CROSS_STRESS_GAP_BUFFER_TOO_SMALL_OR_INVALID")
        floors = []
        for p in positions:
            p.validate()
            stop_ref = p.entry_fair_price * _STOP_MULTIPLIER
            if p.current_fair_price >= stop_ref:
                return blocked("V40_CROSS_STRESS_STOP_REFERENCE_ALREADY_REACHED")
            floors.append(stop_ref / p.current_fair_price - D(1) + gap)
        floor = max(floors)
        if floor > _MAX_MODEL_SHOCK:
            return blocked("V40_CROSS_STRESS_FLOOR_OUTSIDE_MODEL_RANGE")
        shock = floor if adverse_fair_price_pct is None else _nonnegative(adverse_fair_price_pct)
        common = dict(
            required_adverse_fair_pct=floor * D(100),
            required_overshoot_pct=gap * D(100),
        )
        if shock < floor:
            return blocked("V40_CROSS_STRESS_BELOW_STOP_GAP_FLOOR",
                           scenario_adverse_fair_pct=shock * D(100), **common)
        if shock > _MAX_MODEL_SHOCK:
            return blocked("V40_CROSS_STRESS_SCENARIO_OUTSIDE_MODEL_RANGE",
                           scenario_adverse_fair_pct=shock * D(100), **common)

        model = assess_cross_margin_shock(
            account=account, positions=positions,
            adverse_fair_price_pct=shock,
        )
        common["scenario_adverse_fair_pct"] = shock * D(100)
        if model.status in {
            "CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
            "CROSS_STRESS_RISK_TIER_CAP_NOT_VERIFIED",
            "CROSS_STRESS_INPUT_INVALID",
            "CROSS_STRESS_DUPLICATE_SYMBOL",
        }:
            return blocked("V40_CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
                           **common)
        if model.status == "CROSS_STRESS_SCENARIO_SURVIVES_NONAUTHORIZING":
            return blocked("V40_STOP_GAP_SCENARIO_SURVIVES_NONAUTHORIZING",
                           scenario_stress_survived=True, **common)
        if model.status in {
            "CROSS_STRESS_INITIAL_MARGIN_INFEASIBLE_NONAUTHORIZING",
            "CROSS_STRESS_LIQUIDATION_THRESHOLD_BREACH_NONAUTHORIZING",
        }:
            return blocked("V40_STOP_GAP_SCENARIO_BREACH_NONAUTHORIZING",
                           **common)
        return blocked("V40_CROSS_STRESS_MODEL_OUTCOME_UNVERIFIED", **common)
    except (ValueError, TypeError, ArithmeticError, InvalidOperation):
        return blocked("V40_CROSS_STRESS_INPUT_INVALID")
