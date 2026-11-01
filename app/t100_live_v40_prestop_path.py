"""Pure offline T100/V40 short liquidation-threshold path witness.

Unlike an end-point-only shock, this evaluates intermediate simultaneous
adverse FAIR-price levels until the FIRST of 1..4 frozen SL75 short stop
references. It models the portfolio holding ALL positions until that first
reference. This is an adverse no-exit scenario, NOT a simulation of stop
execution, partial liquidation, impact, margin mode changes, funding drift,
tier transitions or actual venue liquidation.

It reuses assess_cross_margin_shock's required account completeness, fair,
current/private cross-mode and stressed-tier booleans. No caller or output
from this module CERTIFIES those booleans. Even a model that survives ALL
sampled points cannot establish venue safety, fill reliability, live
authorization or that MEXC recognizes a proposed cross-margin entry.

No exchange, HTTP, credentials, DB, Render, orders, POST or external effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Sequence

from app.t100_live_cross_margin_stress import (
    CrossAccount, ShortCrossPosition, assess_cross_margin_shock,
)

D = Decimal
_STOP_MULTIPLIER = D("1.75")
_MAX_SHOCK = D("2.0")
_MAX_SAMPLES = 400


@dataclass(frozen=True)
class V40PreStopPath:
    status: str
    modeled_first_stop_reference_pct: D | None = None
    modeled_first_threshold_breach_pct: D | None = None
    last_evaluated_shock_pct: D | None = None
    evaluated_shock_points: int = 0
    model_no_threshold_breach_on_sampled_path: bool = False
    initial_margin_feasible_in_model: bool = False
    current_stressed_tier_evidence_accepted_by_model: bool = False
    verified_future_stressed_tiers: bool = False
    account_wide_cross_collateral_certified: bool = False
    liquidation_path_certified_by_venue: bool = False
    stop_execution_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _step_pct(raw: Any) -> D:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_STEP")
    try:
        x = D(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_STEP") from None
    if not x.is_finite() or not D("0.5") <= x <= D("5"):
        raise ValueError("INVALID_STEP")
    return x


def observe_v40_prestop_model_path(
    *, account: CrossAccount, positions: Sequence[ShortCrossPosition],
    sample_step_pct: Any = D("1.0"),
) -> V40PreStopPath:
    """Check intermediate shock points, never issue/authorize any order.

    The first potential stop reference is based on entry fair * 1.75 vs
    current fair. Stops may actually trigger on another price source and
    fill elsewhere: no conclusion here proves protection in live trading.
    """
    def report(status: str, **kwargs: Any) -> V40PreStopPath:
        return V40PreStopPath(status=status, **kwargs)

    if (
        type(account) is not CrossAccount or
        not isinstance(positions, (tuple, list)) or
        not 1 <= len(positions) <= 4 or
        any(type(p) is not ShortCrossPosition for p in positions)
    ):
        return report("V40_PRESTOP_INPUT_INVALID")

    try:
        account.validate()
        step = _step_pct(sample_step_pct) / D(100)
        floors: list[D] = []
        for p in positions:
            p.validate()
            floor = (
                _STOP_MULTIPLIER * p.entry_fair_price / p.current_fair_price
            ) - D(1)
            if floor <= 0:
                return report("V40_PRESTOP_STOP_REFERENCE_ALREADY_REACHED")
            floors.append(floor)
    except (ValueError, TypeError, ArithmeticError, InvalidOperation):
        return report("V40_PRESTOP_INPUT_INVALID")

    if len({p.symbol for p in positions}) != len(positions):
        return report("V40_PRESTOP_DUPLICATE_SYMBOL")
    boundary = min(floors)
    common = {"modeled_first_stop_reference_pct": boundary * D(100)}
    if boundary > _MAX_SHOCK:
        return report("V40_PRESTOP_OUTSIDE_MODEL_SHOCK_RANGE", **common)
    if not (
        account.account_inventory_complete_verified
        and account.usdt_wallet_economic_witness_verified
        and account.risk_limits_fresh_verified
        and all(
            p.stressed_tier_upper_bound_verified
            and p.cross_margin_verified
            and p.fair_price_verified for p in positions
        )
    ):
        return report("V40_PRESTOP_EXCHANGE_EVIDENCE_INCOMPLETE", **common)

    points = int((boundary / step).to_integral_value(rounding=ROUND_CEILING))
    if not 1 <= points <= _MAX_SAMPLES:
        return report("V40_PRESTOP_SAMPLING_BOUNDS_UNVERIFIED", **common)
    previous: D | None = None
    for idx in range(1, points + 1):
        shock = min(boundary, D(idx) * step)
        if previous is not None and shock <= previous:
            return report("V40_PRESTOP_SAMPLE_SEQUENCE_INVALID", **common)
        previous = shock
        modeled = assess_cross_margin_shock(
            account=account, positions=positions,
            adverse_fair_price_pct=shock,
        )
        observed = dict(
            **common, last_evaluated_shock_pct=shock * D(100),
            evaluated_shock_points=idx,
            current_stressed_tier_evidence_accepted_by_model=True,
        )
        if modeled.status == "CROSS_STRESS_INITIAL_MARGIN_INFEASIBLE_NONAUTHORIZING":
            return report("V40_PRESTOP_INITIAL_MARGIN_INFEASIBLE", **observed)
        if modeled.status == "CROSS_STRESS_LIQUIDATION_THRESHOLD_BREACH_NONAUTHORIZING":
            return report(
                "V40_PRESTOP_MODELED_THRESHOLD_BREACH_AT_OR_BEFORE_FIRST_STOP",
                modeled_first_threshold_breach_pct=shock * D(100),
                initial_margin_feasible_in_model=True, **observed,
            )
        if modeled.status != "CROSS_STRESS_SCENARIO_SURVIVES_NONAUTHORIZING":
            return report("V40_PRESTOP_MODEL_OR_TIER_BECAME_UNVERIFIED",
                          **observed)
    return report(
        "V40_PRESTOP_SAMPLED_PATH_NO_MODEL_BREACH_NONAUTHORIZING",
        **common, last_evaluated_shock_pct=boundary * D(100),
        evaluated_shock_points=points,
        current_stressed_tier_evidence_accepted_by_model=True,
        model_no_threshold_breach_on_sampled_path=True,
        initial_margin_feasible_in_model=True,
    )
