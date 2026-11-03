"""Conservative MEXC cross-margin portfolio shock calculator for frozen T100/V40.

PURE/OFFLINE: no HTTP, DB, order, leverage change or authorization.

MEXC documentation (consulted 2026-10-28):
 - https://www.mexc.com/support/article/faq-on-liquidation-for-futures-trading-8123281969561
 - https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits
 - https://www.mexc.co/api-docs/futures/websocket-api/contract-data

Cross equity considers wallet, isolated and order margin, and cross-position
unrealized PNL. MEXC liquidates when equity <= maintenance margin + liquidation
fees, using FAIR prices. Tiered partial liquidation and changes during a price
path are not simulated here. An endpoint's current risk tier does NOT verify
the worst tier reached under a future adverse shock. The caller must separately
verify a stressed-tier maintenance upper bound for EACH exact symbol/position;
this function never substitutes a constant 5% rate or a guessed tier.

No model result is certification of a live venue, a liquidation guarantee or
permission to enable the adapter. Only all-short USDT linear positions are
supported. Full V40 strategy exposure can exceed account equity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

D = Decimal


def _number(value: Any, name: str, *, positive: bool = False) -> D:
    if type(value) is bool:
        raise ValueError("INVALID_" + name)
    try:
        x = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("INVALID_" + name) from exc
    if not x.is_finite() or (x <= 0 if positive else x < 0):
        raise ValueError("INVALID_" + name)
    return x


@dataclass(frozen=True)
class ShortCrossPosition:
    symbol: str
    entry_fair_price: D
    current_fair_price: D
    contracts: D
    contract_size: D
    configured_leverage: int
    tier_max_position_notional_usdt: D
    worst_stress_mmr: D
    liquidation_fee_rate: D
    # A documented, independently inspected stressed-tier upper bound must
    # cover this position at the stress price, not just at its current price.
    stressed_tier_upper_bound_verified: bool
    cross_margin_verified: bool
    fair_price_verified: bool

    def validate(self) -> None:
        if (
            type(self.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", self.symbol) is None
            or self.symbol in {"BTC_USDT", "ETH_USDT"}
            or type(self.configured_leverage) is not int
            or not 1 <= self.configured_leverage <= 200
        ):
            raise ValueError("INVALID_CROSS_POSITION_IDENTITY_OR_LEVERAGE")
        for name in (
            "entry_fair_price", "current_fair_price", "contracts",
            "contract_size", "tier_max_position_notional_usdt",
        ):
            _number(getattr(self, name), name.upper(), positive=True)
        for name in ("worst_stress_mmr", "liquidation_fee_rate"):
            rate = _number(getattr(self, name), name.upper())
            if not D(0) < rate < D(1):
                raise ValueError("INVALID_" + name.upper())
        for name in (
            "stressed_tier_upper_bound_verified", "cross_margin_verified",
            "fair_price_verified",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError("INVALID_" + name.upper())


@dataclass(frozen=True)
class CrossAccount:
    # Wallet excluding unrealized PNL; all four positions share this balance.
    wallet_balance_usdt: D
    isolated_position_margin_usdt: D
    pending_order_margin_usdt: D
    committed_cash_reserve_usdt: D
    entry_and_future_fees_reserve_usdt: D
    additional_adverse_cashflow_usdt: D
    account_inventory_complete_verified: bool
    usdt_wallet_economic_witness_verified: bool
    risk_limits_fresh_verified: bool

    def validate(self) -> None:
        _number(self.wallet_balance_usdt, "WALLET", positive=True)
        for field in (
            "isolated_position_margin_usdt",
            "pending_order_margin_usdt",
            "committed_cash_reserve_usdt",
            "entry_and_future_fees_reserve_usdt",
            "additional_adverse_cashflow_usdt",
        ):
            _number(getattr(self, field), field.upper())
        for field in (
            "account_inventory_complete_verified",
            "usdt_wallet_economic_witness_verified",
            "risk_limits_fresh_verified",
        ):
            if type(getattr(self, field)) is not bool:
                raise ValueError("INVALID_" + field.upper())


@dataclass(frozen=True)
class CrossShockReport:
    status: str
    gross_entry_notional_usdt: D = D(0)
    gross_stressed_notional_usdt: D = D(0)
    configured_initial_margin_usdt: D = D(0)
    available_initial_margin_usdt: D = D(0)
    stressed_unrealized_pnl_usdt: D = D(0)
    stressed_cross_equity_usdt: D = D(0)
    stressed_maintenance_margin_usdt: D = D(0)
    stressed_liquidation_fee_usdt: D = D(0)
    coverage_ratio: D | None = None
    scenario_stress_pct: D = D(0)
    modeled_stress_survived: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def assess_cross_margin_shock(
    *, account: CrossAccount, positions: Sequence[ShortCrossPosition],
    adverse_fair_price_pct: D = D("0.20"),
) -> CrossShockReport:
    """Pure post-shock accounting under a simultaneous upward FAIR-price jump.

    Uses conservatively MAX(entry, stressed fair) notional times a caller's
    verified worst-case MMR bound, plus a separate liquidating-fee reserve.
    A clear 'survived model' is only conditional on its assumptions and is
    intentionally never live authorization.
    """
    def blocked(reason: str) -> CrossShockReport:
        return CrossShockReport(status=reason)

    if not isinstance(account, CrossAccount) or not isinstance(positions, (tuple, list)):
        return blocked("CROSS_STRESS_INPUT_INVALID")
    if not 1 <= len(positions) <= 4 or any(type(p) is not ShortCrossPosition for p in positions):
        return blocked("CROSS_STRESS_ONE_TO_FOUR_SHORTS_REQUIRED")
    try:
        account.validate()
        shock = _number(adverse_fair_price_pct, "ADVERSE_FAIR_SHOCK")
        if not D(0) < shock <= D(2):
            return blocked("CROSS_STRESS_SHOCK_INVALID")
        for p in positions:
            p.validate()
        if len({p.symbol for p in positions}) != len(positions):
            return blocked("CROSS_STRESS_DUPLICATE_SYMBOL")
        if not (
            account.account_inventory_complete_verified
            and account.usdt_wallet_economic_witness_verified
            and account.risk_limits_fresh_verified
            and all(p.stressed_tier_upper_bound_verified and
                    p.cross_margin_verified and p.fair_price_verified
                    for p in positions)
        ):
            return blocked("CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE")

        entry_total = D(0)
        stressed_total = D(0)
        initial_total = D(0)
        pnl = D(0)
        maintenance = D(0)
        liquidation_fees = D(0)
        for p in positions:
            qty = p.contracts * p.contract_size
            entry_value = p.entry_fair_price * qty
            current = p.current_fair_price
            stressed = current * (D(1) + shock)
            stressed_value = stressed * qty
            if (
                max(entry_value, stressed_value) >
                p.tier_max_position_notional_usdt
            ):
                return blocked("CROSS_STRESS_RISK_TIER_CAP_NOT_VERIFIED")
            entry_total += entry_value
            stressed_total += stressed_value
            initial_total += entry_value / D(p.configured_leverage)
            pnl += (p.entry_fair_price - stressed) * qty
            maintenance += max(entry_value, stressed_value) * p.worst_stress_mmr
            liquidation_fees += stressed_value * p.liquidation_fee_rate

        available = (
            account.wallet_balance_usdt
            - account.isolated_position_margin_usdt
            - account.pending_order_margin_usdt
            - account.committed_cash_reserve_usdt
            - account.entry_and_future_fees_reserve_usdt
        )
        equity = (
            account.wallet_balance_usdt
            - account.isolated_position_margin_usdt
            - account.pending_order_margin_usdt
            + pnl
            - account.entry_and_future_fees_reserve_usdt
            - account.additional_adverse_cashflow_usdt
        )
        required = maintenance + liquidation_fees
        ratio = equity / required if required > 0 else None
        margin_ok = initial_total <= available
        survives = margin_ok and equity > required
        return CrossShockReport(
            status=(
                "CROSS_STRESS_SCENARIO_SURVIVES_NONAUTHORIZING"
                if survives else
                "CROSS_STRESS_INITIAL_MARGIN_INFEASIBLE_NONAUTHORIZING"
                if not margin_ok else
                "CROSS_STRESS_LIQUIDATION_THRESHOLD_BREACH_NONAUTHORIZING"
            ),
            gross_entry_notional_usdt=entry_total,
            gross_stressed_notional_usdt=stressed_total,
            configured_initial_margin_usdt=initial_total,
            available_initial_margin_usdt=available,
            stressed_unrealized_pnl_usdt=pnl,
            stressed_cross_equity_usdt=equity,
            stressed_maintenance_margin_usdt=maintenance,
            stressed_liquidation_fee_usdt=liquidation_fees,
            coverage_ratio=ratio,
            scenario_stress_pct=shock * D(100),
            modeled_stress_survived=survives,
        )
    except (ValueError, TypeError, InvalidOperation, ArithmeticError):
        return blocked("CROSS_STRESS_INPUT_INVALID")
