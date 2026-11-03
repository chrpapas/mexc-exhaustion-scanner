"""Fail-closed T100 execution-mode and risk controls.

This is a readiness boundary, NOT a live execution adapter. T100's current
portfolio implementation records synthetic fills; enabling live orders against
those tables would create untracked exchange risk.

No code in this module can submit an order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

LIVE_CONFIRMATION = "I_UNDERSTAND_MEXC_REAL_FUNDS_AT_RISK"
LIVE_ADAPTER_STATUS = "NOT_IMPLEMENTED"


def _flag(values: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be an explicit boolean")


def _positive_limit(values: Mapping[str, str], key: str) -> Decimal | None:
    raw = values.get(key, "").strip()
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{key} is not a decimal number") from error
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{key} must be finite and positive")
    return number


@dataclass(frozen=True)
class T100ExecutionControls:
    mode: str
    live_orders_enabled: bool
    kill_switch_engaged: bool
    operator_confirmed: bool
    max_position_notional_usdt: Decimal | None
    max_total_notional_usdt: Decimal | None
    max_daily_loss_usdt: Decimal | None
    leverage: int

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "T100ExecutionControls":
        env = os.environ if values is None else values
        mode = env.get("T100_EXECUTION_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError("T100_EXECUTION_MODE must be paper or live")
        try:
            leverage = int(env.get("T100_LIVE_LEVERAGE", "1"))
        except ValueError as error:
            raise ValueError("T100_LIVE_LEVERAGE must be an integer") from error
        if not 1 <= leverage <= 2:
            raise ValueError("T100_LIVE_LEVERAGE must be between 1 and 2")
        return cls(
            mode=mode,
            live_orders_enabled=_flag(env, "T100_LIVE_ORDER_API_ENABLED"),
            kill_switch_engaged=_flag(env, "T100_LIVE_KILL_SWITCH", True),
            operator_confirmed=env.get("T100_LIVE_OPERATOR_CONFIRM", "") == LIVE_CONFIRMATION,
            max_position_notional_usdt=_positive_limit(env, "T100_LIVE_MAX_POSITION_NOTIONAL_USDT"),
            max_total_notional_usdt=_positive_limit(env, "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT"),
            max_daily_loss_usdt=_positive_limit(env, "T100_LIVE_MAX_DAILY_LOSS_USDT"),
            leverage=leverage,
        )

    def readiness_failures(self) -> tuple[str, ...]:
        """Operator configuration checks; passing does NOT authorize trading."""
        if self.mode == "paper":
            return ()
        problems: list[str] = []
        if not self.live_orders_enabled:
            problems.append("order_api_switch_off")
        if self.kill_switch_engaged:
            problems.append("kill_switch_engaged")
        if not self.operator_confirmed:
            problems.append("operator_confirmation_missing")
        if self.max_position_notional_usdt is None:
            problems.append("missing_position_limit")
        if self.max_total_notional_usdt is None:
            problems.append("missing_portfolio_limit")
        if self.max_daily_loss_usdt is None:
            problems.append("missing_daily_loss_limit")
        if (self.max_position_notional_usdt is not None
                and self.max_total_notional_usdt is not None
                and self.max_position_notional_usdt > self.max_total_notional_usdt):
            problems.append("position_limit_exceeds_portfolio_limit")
        return tuple(problems)


def require_paper_portfolio(controls: T100ExecutionControls) -> None:
    """Call before connecting to / migrating the production paper database.

    A future LIVE adapter must have independent order, position, and ledger
    persistence, exchange fill reconciliation, native protective orders,
    liquidity gating and well-tested stop/kill-switch behavior.
    """
    if controls.mode == "paper":
        return
    failures = controls.readiness_failures()
    details = ",".join(failures) if failures else "operator_gates_passed"
    raise RuntimeError(
        "T100_LIVE_EXECUTION_BLOCKED: this trader is PAPER ONLY; "
        f"live_adapter={LIVE_ADAPTER_STATUS}; safety={details}; "
        "no real orders have been submitted"
    )
