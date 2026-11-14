"""UNRELEASED: sequential V40 lot plans -> independently supplied MEXC cross-risk model.

The frozen HYBRID strategy is not equivalent to the tiny one-position live
canary. Historical pooled 5x shock budgeting is NOT a venue leverage
setting. This bridge requires *per-symbol* lot identity, independently
observed cross leverage and stress-tier inputs before invoking the existing
conservative 20%, SL75 and SL75+5% fair-price stress calculator.

Every result is nonauthorizing. Model survival is NOT an actual MEXC
liquidation-price certificate, a native-stop fill guarantee, or replay parity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Sequence

from app.t100_v40_unreleased_sequential_lot_margin import (
    V40SequentialLotMarginProjection,
)
from app.t100_live_cross_margin_stress import (
    CrossAccount,
    CrossShockReport,
    ShortCrossPosition,
    assess_cross_margin_shock,
)

_SHOCKS=(D("0.20"),D("0.75"),D("0.80"))


class V40CrossRiskBridgeHalted(ValueError):
    pass


@dataclass(frozen=True)
class V40CrossRiskBridgeReport:
    status: str
    expected_portfolio_notional_usdt: D
    independently_provided_portfolio_entry_notional_usdt: D
    shock20: CrossShockReport
    stop75: CrossShockReport
    stop75_gap5: CrossShockReport
    all_symbols_and_lots_reconciled: bool
    all_scenarios_survive_model: bool
    exchange_liquidation_certified: bool = False
    live_strategy_equivalence_certified: bool = False
    native_stop_execution_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def compare_v40_sequential_lots_with_cross_stress(
    *,
    sequential: V40SequentialLotMarginProjection,
    positions: Sequence[ShortCrossPosition],
    account: CrossAccount,
) -> V40CrossRiskBridgeReport:
    """Only accepts the exact 1–4 sequentially clipped, lot-rounded model.

    The caller must independently authenticate contract sizes, fair entry
    prices, margin/leverage, current inventory and the UPPER risk-tier
    maintenance bound reached under the stressed prices. Without that
    evidence, a shock calculation MUST fail shut, not silently assume 5%.
    """
    if (
        type(sequential) is not V40SequentialLotMarginProjection
        or sequential.status!="V40_SEQUENTIAL_LOT_AND_MARGIN_OFFLINE_NONAUTHORIZING"
        or sequential.all_lots_quantized is not True
        or sequential.historical_full_exit_replay_certified is not False
        or sequential.exchange_contract_specs_authenticated is not False
        or sequential.live_authorization is not False
        or sequential.mexc_posts!=0 or sequential.database_writes!=0
        or type(sequential.lot_plans) is not tuple
        or not 1<=len(sequential.lot_plans)<=4
        or type(positions) not in (tuple,list)
        or len(positions)!=len(sequential.lot_plans)
        or type(account) is not CrossAccount
        or sequential.margin.live_authorization is not False
        or sequential.margin.entry_allowed is not False
    ):
        raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_SOURCE_EVIDENCE_INVALID")

    lots=sequential.lot_plans
    if any(type(p) is not ShortCrossPosition for p in positions):
        raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_POSITION_SHAPE_INVALID")
    if (
        len({p.symbol for p in positions})!=len(positions)
        or len({s.symbol for s in lots})!=len(lots)
        or {s.symbol for s in lots}!={p.symbol for p in positions}
    ):
        raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_SYMBOL_SET_MISMATCH")
    actual_total=D(0)
    for s in lots:
        p=next(x for x in positions if x.symbol==s.symbol)
        try:
            p.validate()
            theoretical=p.entry_fair_price*p.contracts*p.contract_size
            modeled=s.plan.lot_quantized_notional_usdt
            if (
                s.plan.admission_action!="ADMIT"
                or s.plan.live_authorization is not False
                or s.plan.live_order_allowed is not False
                or s.plan.exchange_leverage_observed!=p.configured_leverage
                or s.plan.volume_contracts!=p.contracts
                or theoretical<=0 or modeled<=0
                # Same frozen single-candle price model. A REAL fill with a
                # different price requires a separate live-risk witness;
                # do not pass mismatched hypothetical notional as cert.
                or abs(theoretical-modeled)>max(D("0.0000001"),modeled*D("0.00000001"))
            ):
                raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_LOT_OR_LEVERAGE_DRIFT")
            actual_total+=theoretical
        except V40CrossRiskBridgeHalted:
            raise
        except Exception:
            raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_LOT_OR_LEVERAGE_DRIFT") from None

    if (
        actual_total!=sequential.margin.portfolio_notional_usdt
        or actual_total<=0
    ):
        raise V40CrossRiskBridgeHalted("V40_CROSS_RISK_PORTFOLIO_TOTAL_MISMATCH")

    reports=tuple(
        assess_cross_margin_shock(
            account=account,positions=positions,adverse_fair_price_pct=shock
        ) for shock in _SHOCKS
    )
    valid=all(
        r.status in (
            "CROSS_STRESS_SCENARIO_SURVIVES_NONAUTHORIZING",
            "CROSS_STRESS_INITIAL_MARGIN_INFEASIBLE_NONAUTHORIZING",
            "CROSS_STRESS_LIQUIDATION_THRESHOLD_BREACH_NONAUTHORIZING",
        )
        for r in reports
    )
    survived=valid and all(r.modeled_stress_survived for r in reports)
    return V40CrossRiskBridgeReport(
        status=(
            "V40_CROSS_RISK_UNVERIFIED_ACCOUNT_OR_TIER_HALT"
            if not valid else
            "V40_CROSS_RISK_THREE_SHOCK_MODELS_SURVIVE_NONAUTHORIZING"
            if survived else
            "V40_CROSS_RISK_ONE_OR_MORE_SHOCKS_FAIL_NONAUTHORIZING"
        ),
        expected_portfolio_notional_usdt=sequential.margin.portfolio_notional_usdt,
        independently_provided_portfolio_entry_notional_usdt=actual_total,
        shock20=reports[0],stop75=reports[1],stop75_gap5=reports[2],
        all_symbols_and_lots_reconciled=True,
        all_scenarios_survive_model=survived,
    )
