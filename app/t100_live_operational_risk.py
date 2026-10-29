"""Pure operational risk kill controls for T100.

No exchange calls and no order authorization. This module consumes already
validated numeric risk observations and trips the same one-way operational
kill latch used by the soak controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from app.t100_live_kill import OperationalKillLatch


class OperationalRiskHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationalRiskLimits:
    max_position_notional_usdt: Decimal
    max_total_notional_usdt: Decimal
    max_daily_loss_usdt: Decimal

    def __post_init__(self) -> None:
        for value in (
            self.max_position_notional_usdt,
            self.max_total_notional_usdt,
            self.max_daily_loss_usdt,
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError("INVALID_OPERATIONAL_RISK_LIMIT")
        if self.max_position_notional_usdt > self.max_total_notional_usdt:
            raise ValueError("POSITION_LIMIT_EXCEEDS_PORTFOLIO_LIMIT")


@dataclass(frozen=True)
class OperationalRiskReport:
    positions_observed: int
    within_position_limit: bool
    within_portfolio_limit: bool
    within_daily_loss_limit: bool
    kill_tripped: bool
    kill_reason: str | None
    status: str
    entry_allowed: bool = False
    live_authorization: bool = False


def _nonnegative(raw: object, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OperationalRiskHalted(f"INVALID_{field}") from exc
    if not value.is_finite() or value < 0:
        raise OperationalRiskHalted(f"INVALID_{field}")
    return value


class OperationalRiskMonitor:
    """One-way quantitative kill monitor.

    A healthy report is evidence only. It never grants entry permission.
    """

    def __init__(
        self,
        limits: OperationalRiskLimits,
        *,
        kill: OperationalKillLatch | None = None,
    ) -> None:
        self.limits = limits
        self.kill = kill if kill is not None else OperationalKillLatch()

    def evaluate(
        self,
        *,
        position_notionals_usdt: Mapping[str, object],
        total_notional_usdt: object,
        daily_loss_usdt: object,
        manual_kill_switch: bool,
    ) -> OperationalRiskReport:
        self.kill.require_untripped()

        if type(manual_kill_switch) is not bool:
            self.kill.trip("RECOVERY_UNVERIFIED")
            raise OperationalRiskHalted("INVALID_MANUAL_KILL_SWITCH")
        if manual_kill_switch:
            self.kill.trip("MANUAL_KILL_SWITCH")
            raise OperationalRiskHalted(
                "OPERATIONAL_RISK_KILLED:MANUAL_KILL_SWITCH"
            )

        if not isinstance(position_notionals_usdt, Mapping):
            self.kill.trip("RECOVERY_UNVERIFIED")
            raise OperationalRiskHalted("INVALID_POSITION_NOTIONAL_MAP")

        notionals: dict[str, Decimal] = {}
        try:
            for key, raw in position_notionals_usdt.items():
                if not isinstance(key, str) or not key or key in notionals:
                    raise OperationalRiskHalted(
                        "INVALID_POSITION_NOTIONAL_IDENTITY"
                    )
                notionals[key] = _nonnegative(raw, "POSITION_NOTIONAL")

            total = _nonnegative(total_notional_usdt, "TOTAL_NOTIONAL")
            loss = _nonnegative(daily_loss_usdt, "DAILY_LOSS")
        except OperationalRiskHalted:
            self.kill.trip("RECOVERY_UNVERIFIED")
            raise

        calculated_total = sum(notionals.values(), Decimal(0))
        tolerance = max(
            Decimal("0.00000001"),
            total * Decimal("0.00000001"),
        )
        if abs(calculated_total - total) > tolerance:
            self.kill.trip("RECOVERY_UNVERIFIED")
            raise OperationalRiskHalted("TOTAL_NOTIONAL_RECONCILIATION_DRIFT")

        position_ok = all(
            value <= self.limits.max_position_notional_usdt
            for value in notionals.values()
        )
        portfolio_ok = total <= self.limits.max_total_notional_usdt
        daily_ok = loss < self.limits.max_daily_loss_usdt

        if not position_ok:
            self.kill.trip("POSITION_EXPOSURE_LIMIT")
            raise OperationalRiskHalted(
                "OPERATIONAL_RISK_KILLED:POSITION_EXPOSURE_LIMIT"
            )
        if not portfolio_ok:
            self.kill.trip("PORTFOLIO_EXPOSURE_LIMIT")
            raise OperationalRiskHalted(
                "OPERATIONAL_RISK_KILLED:PORTFOLIO_EXPOSURE_LIMIT"
            )
        if not daily_ok:
            self.kill.trip("DAILY_LOSS_LIMIT")
            raise OperationalRiskHalted(
                "OPERATIONAL_RISK_KILLED:DAILY_LOSS_LIMIT"
            )

        snap = self.kill.snapshot()
        return OperationalRiskReport(
            positions_observed=len(notionals),
            within_position_limit=True,
            within_portfolio_limit=True,
            within_daily_loss_limit=True,
            kill_tripped=snap.tripped,
            kill_reason=snap.first_reason,
            status="WITHIN_LIMITS_NON_AUTHORIZING",
        )

    def report_after_kill(self) -> OperationalRiskReport:
        snap = self.kill.snapshot()
        return OperationalRiskReport(
            positions_observed=0,
            within_position_limit=False,
            within_portfolio_limit=False,
            within_daily_loss_limit=False,
            kill_tripped=snap.tripped,
            kill_reason=snap.first_reason,
            status="OPERATIONAL_RISK_KILLED" if snap.tripped else "NOT_EVALUATED",
        )
