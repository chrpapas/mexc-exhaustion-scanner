from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

PUBLIC_LEDGER_RISK_TIERS = frozenset({"standard", "high_risk"})


HORIZONS: tuple[tuple[int, str], ...] = (
    (24, "1D"),
    (48, "2D"),
    (72, "3D"),
    (168, "7D"),
)

# Public subscriber risk thresholds. -400% is retained in the raw CSV/history but
# the current strategy comparison/report is standardized on -50/-100/-200/-300.
BREACHES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (50, ("adverse_50_at",)),
    (100, ("adverse_100_at", "isolated_100_breach_at")),
    (200, ("adverse_200_path_at", "adverse_200_breach_at")),
    (300, ("adverse_300_path_at", "adverse_300_breach_at")),
    (400, ("adverse_400_path_at", "cross_400_breach_at")),
)


@dataclass(frozen=True, slots=True)
class LedgerHorizon:
    hours: int
    label: str
    return_pct: float | None
    price: float | None


@dataclass(frozen=True, slots=True)
class LedgerBreach:
    adverse_limit_pct: int
    occurred_at: datetime | None
    hours_after_signal: float | None


@dataclass(frozen=True, slots=True)
class LedgerStrategyOutcome:
    strategy: str
    eligible: bool
    state: str
    effective_at: datetime | None
    return_pct: float | None
    target_at: datetime | None
    target_hours: float | None
    deepest_breach_before_effective_pct: int | None
    breach_50_before_effective: bool
    breach_100_before_effective: bool
    breach_200_before_effective: bool
    breach_300_before_effective: bool


@dataclass(frozen=True, slots=True)
class SignalLedgerItem:
    episode_id: int
    symbol: str
    risk_tier: str
    confirmed_at: datetime
    signal_price: float
    observed_at: datetime
    target_5_at: datetime | None
    time_to_target_5_hours: float | None
    path_mae_before_target_5: float | None
    path_mae_before_target_5_at: datetime | None
    target_20_at: datetime | None
    time_to_target_20_hours: float | None
    first_profit_at: datetime | None
    current_return_pct: float | None
    horizons: tuple[LedgerHorizon, ...]
    breaches: tuple[LedgerBreach, ...]

    @property
    def deepest_breach_pct(self) -> int | None:
        values = [item.adverse_limit_pct for item in self.breaches if item.occurred_at is not None]
        return max(values) if values else None

    @property
    def first_100_breach_at(self) -> datetime | None:
        for breach in self.breaches:
            if breach.adverse_limit_pct == 100:
                return breach.occurred_at
        return None

    def breach_at(self, threshold: int) -> datetime | None:
        for breach in self.breaches:
            if breach.adverse_limit_pct == threshold:
                return breach.occurred_at
        return None

    def breach_before(self, threshold: int, cutoff: datetime | None) -> bool:
        event_at = self.breach_at(threshold)
        return event_at is not None and cutoff is not None and event_at <= cutoff

    def deepest_breach_before(self, cutoff: datetime | None, *, max_threshold: int = 300) -> int | None:
        if cutoff is None:
            return None
        values = [
            breach.adverse_limit_pct
            for breach in self.breaches
            if breach.adverse_limit_pct <= max_threshold
            and breach.occurred_at is not None
            and breach.occurred_at <= cutoff
        ]
        return max(values) if values else None

    @property
    def target_5_before_100_breach(self) -> bool | None:
        breach = self.first_100_breach_at
        target = self.target_5_at
        if target is None and breach is None:
            return None
        if target is None:
            return False
        if breach is None:
            return True
        return target < breach

    @property
    def target_before_100_breach(self) -> bool | None:
        breach = self.first_100_breach_at
        target = self.target_20_at
        if target is None and breach is None:
            return None
        if target is None:
            return False
        if breach is None:
            return True
        return target < breach

    @property
    def latest_known_return_pct(self) -> float | None:
        values = [h.return_pct for h in reversed(self.horizons) if h.return_pct is not None]
        if values:
            return values[0]
        return self.current_return_pct

    @property
    def headline_status(self) -> str:
        target_race = self.target_before_100_breach
        deepest = self.deepest_breach_pct
        if target_race is True:
            if deepest is not None and self.first_100_breach_at and self.target_20_at and self.first_100_breach_at > self.target_20_at:
                return "target_then_breach"
            return "target_hit"
        if target_race is False and self.first_100_breach_at is not None:
            return f"breach_{deepest or 100}"
        latest = self.latest_known_return_pct
        if latest is None:
            return "pending"
        if latest > 0:
            return "profitable_below_target"
        return "safe_negative"

    def _strategy_outcome(
        self,
        *,
        strategy: str,
        eligible: bool,
        target_at: datetime | None,
        target_return: float | None,
        fixed_horizon_hours: int | None = None,
    ) -> LedgerStrategyOutcome:
        if not eligible:
            return LedgerStrategyOutcome(
                strategy=strategy,
                eligible=False,
                state="not_eligible",
                effective_at=None,
                return_pct=None,
                target_at=None,
                target_hours=None,
                deepest_breach_before_effective_pct=None,
                breach_50_before_effective=False,
                breach_100_before_effective=False,
                breach_200_before_effective=False,
                breach_300_before_effective=False,
            )

        effective_at: datetime
        outcome_return: float | None
        state: str
        actual_target_at: datetime | None = None
        target_hours: float | None = None

        if fixed_horizon_hours is not None:
            horizon_at = self.confirmed_at + timedelta(hours=fixed_horizon_hours)
            horizon = next((h for h in self.horizons if h.hours == fixed_horizon_hours), None)
            if horizon is not None and horizon.return_pct is not None and self.observed_at >= horizon_at:
                effective_at = horizon_at
                outcome_return = horizon.return_pct
                state = "closed_win" if outcome_return > 0 else "closed_loss"
            else:
                effective_at = min(self.observed_at, horizon_at)
                outcome_return = self.current_return_pct
                state = "tracking"
        elif target_at is not None and target_return is not None and target_at <= self.observed_at:
            effective_at = target_at
            outcome_return = target_return
            actual_target_at = target_at
            target_hours = _elapsed_hours(self.confirmed_at, target_at)
            state = "target_hit"
        else:
            effective_at = self.observed_at
            outcome_return = self.current_return_pct
            state = "open"

        flags = {
            threshold: self.breach_before(threshold, effective_at)
            for threshold in (50, 100, 200, 300)
        }
        deepest = max((threshold for threshold, hit in flags.items() if hit), default=None)
        return LedgerStrategyOutcome(
            strategy=strategy,
            eligible=True,
            state=state,
            effective_at=effective_at,
            return_pct=outcome_return,
            target_at=actual_target_at,
            target_hours=target_hours,
            deepest_breach_before_effective_pct=deepest,
            breach_50_before_effective=flags[50],
            breach_100_before_effective=flags[100],
            breach_200_before_effective=flags[200],
            breach_300_before_effective=flags[300],
        )

    @property
    def tp5_strategy(self) -> LedgerStrategyOutcome:
        return self._strategy_outcome(
            strategy="tp5_frequent",
            eligible=True,
            target_at=self.target_5_at,
            target_return=0.05,
        )

    @property
    def tp20_strategy(self) -> LedgerStrategyOutcome:
        return self._strategy_outcome(
            strategy="tp20_high_no_timeout",
            eligible=self.risk_tier == "high_risk",
            target_at=self.target_20_at,
            target_return=0.20,
        )

    @property
    def standard_7d_strategy(self) -> LedgerStrategyOutcome:
        return self._strategy_outcome(
            strategy="standard_7d",
            eligible=self.risk_tier == "standard",
            target_at=None,
            target_return=None,
            fixed_horizon_hours=168,
        )


@dataclass(frozen=True, slots=True)
class SignalLedger:
    generated_at: datetime
    items: tuple[SignalLedgerItem, ...]

    @property
    def total(self) -> int:
        return len(self.items)

    def count_status(self, *statuses: str) -> int:
        wanted = set(statuses)
        return sum(item.headline_status in wanted for item in self.items)

    def count_breach(self, threshold: int) -> int:
        return sum(
            any(b.adverse_limit_pct == threshold and b.occurred_at is not None for b in item.breaches)
            for item in self.items
        )

    def by_risk(self, risk_tier: str) -> tuple[SignalLedgerItem, ...]:
        return tuple(item for item in self.items if item.risk_tier == risk_tier)


def _horizon_price(entry_price: float, return_pct: float | None) -> float | None:
    if return_pct is None:
        return None
    return entry_price * (1.0 - return_pct)


def _elapsed_hours(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _earliest(row: dict[str, Any], *keys: str) -> datetime | None:
    values = [row.get(key) for key in keys if row.get(key) is not None]
    return min(values) if values else None


def build_signal_ledger(rows: Iterable[dict[str, Any]], *, generated_at: datetime) -> SignalLedger:
    items: list[SignalLedgerItem] = []
    for row in rows:
        risk_tier = str(row.get("risk_tier") or "standard")
        if risk_tier not in PUBLIC_LEDGER_RISK_TIERS:
            continue
        entry = float(row["entry_price"])
        confirmed_at = row["confirmed_at"]
        target_5_at = row.get("target_5_at")
        target_at = _earliest(row, "target_20_path_at", "target_20_at")

        horizons: list[LedgerHorizon] = []
        for hours, label in HORIZONS:
            value = row.get(f"return_{hours}h_pct")
            ret = float(value) if value is not None else None
            horizons.append(LedgerHorizon(hours, label, ret, _horizon_price(entry, ret)))

        breaches: list[LedgerBreach] = []
        for threshold, keys in BREACHES:
            occurred_at = _earliest(row, *keys)
            breaches.append(
                LedgerBreach(
                    adverse_limit_pct=threshold,
                    occurred_at=occurred_at,
                    hours_after_signal=_elapsed_hours(confirmed_at, occurred_at),
                )
            )

        current_return = row.get("current_return_pct")
        items.append(
            SignalLedgerItem(
                episode_id=int(row["episode_id"]),
                symbol=str(row["symbol"]),
                risk_tier=risk_tier,
                confirmed_at=confirmed_at,
                signal_price=entry,
                observed_at=generated_at,
                target_5_at=target_5_at,
                time_to_target_5_hours=_elapsed_hours(confirmed_at, target_5_at),
                path_mae_before_target_5=(
                    float(row["path_mae_before_target_5"])
                    if row.get("path_mae_before_target_5") is not None else None
                ),
                path_mae_before_target_5_at=row.get("path_mae_before_target_5_at"),
                target_20_at=target_at,
                time_to_target_20_hours=_elapsed_hours(confirmed_at, target_at),
                first_profit_at=row.get("first_profit_at"),
                current_return_pct=float(current_return) if current_return is not None else None,
                horizons=tuple(horizons),
                breaches=tuple(breaches),
            )
        )

    items.sort(key=lambda item: item.confirmed_at, reverse=True)
    return SignalLedger(generated_at=generated_at, items=tuple(items))


def _flag(value: bool) -> str:
    return "yes" if value else "no"


def signal_ledger_csv(ledger: SignalLedger) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "episode_id",
        "symbol",
        "risk_tier",
        "signal_time_utc",
        "signal_price",
        # Current subscriber strategies first.
        "tp5_status",
        "tp5_return_or_mark_pct",
        "tp5_target_at_utc",
        "tp5_time_to_target_hours",
        "tp5_deepest_breach_before_target_or_mark_pct",
        "tp5_breach_50_before_target_or_mark",
        "tp5_breach_100_before_target_or_mark",
        "tp5_breach_200_before_target_or_mark",
        "tp5_breach_300_before_target_or_mark",
        "tp20_status",
        "tp20_return_or_mark_pct",
        "tp20_target_at_utc",
        "tp20_time_to_target_hours",
        "tp20_deepest_breach_before_target_or_mark_pct",
        "tp20_breach_50_before_target_or_mark",
        "tp20_breach_100_before_target_or_mark",
        "tp20_breach_200_before_target_or_mark",
        "tp20_breach_300_before_target_or_mark",
        "standard_7d_status",
        "standard_7d_return_or_mark_pct",
        "standard_7d_deepest_breach_before_exit_or_mark_pct",
        "standard_7d_breach_50_before_exit_or_mark",
        "standard_7d_breach_100_before_exit_or_mark",
        "standard_7d_breach_200_before_exit_or_mark",
        "standard_7d_breach_300_before_exit_or_mark",
        # Raw/audit fields retained after the strategy view.
        "headline_status",
        "current_return_pct",
        "mae_before_target_5_pct",
        "mae_before_target_5_at_utc",
        "target_5_before_100_breach",
        "price_1d",
        "return_1d_pct",
        "price_2d",
        "return_2d_pct",
        "price_3d",
        "return_3d_pct",
        "price_7d",
        "return_7d_pct",
        "breach_50_at_utc",
        "breach_50_hours",
        "breach_100_at_utc",
        "breach_100_hours",
        "breach_200_at_utc",
        "breach_200_hours",
        "breach_300_at_utc",
        "breach_300_hours",
        "breach_400_at_utc",
        "breach_400_hours",
    ])

    for item in ledger.items:
        horizons = {h.hours: h for h in item.horizons}
        breaches = {b.adverse_limit_pct: b for b in item.breaches}
        tp5 = item.tp5_strategy
        tp20 = item.tp20_strategy
        swing = item.standard_7d_strategy

        def strategy_values(outcome: LedgerStrategyOutcome, *, include_target: bool) -> list[Any]:
            if not outcome.eligible:
                if include_target:
                    return ["not_eligible", "", "", "", "", "", "", "", ""]
                return ["not_eligible", "", "", "", "", "", ""]
            base: list[Any] = [
                outcome.state,
                outcome.return_pct if outcome.return_pct is not None else "",
            ]
            if include_target:
                base.extend([
                    outcome.target_at.isoformat() if outcome.target_at else "",
                    outcome.target_hours if outcome.target_hours is not None else "",
                ])
            base.extend([
                outcome.deepest_breach_before_effective_pct or "",
                _flag(outcome.breach_50_before_effective),
                _flag(outcome.breach_100_before_effective),
                _flag(outcome.breach_200_before_effective),
                _flag(outcome.breach_300_before_effective),
            ])
            return base

        writer.writerow([
            item.episode_id,
            item.symbol,
            item.risk_tier,
            item.confirmed_at.isoformat(),
            item.signal_price,
            *strategy_values(tp5, include_target=True),
            *strategy_values(tp20, include_target=True),
            *strategy_values(swing, include_target=False),
            item.headline_status,
            item.current_return_pct if item.current_return_pct is not None else "",
            item.path_mae_before_target_5 if item.path_mae_before_target_5 is not None else "",
            item.path_mae_before_target_5_at.isoformat() if item.path_mae_before_target_5_at else "",
            item.target_5_before_100_breach if item.target_5_before_100_breach is not None else "",
            horizons[24].price if horizons[24].price is not None else "",
            horizons[24].return_pct if horizons[24].return_pct is not None else "",
            horizons[48].price if horizons[48].price is not None else "",
            horizons[48].return_pct if horizons[48].return_pct is not None else "",
            horizons[72].price if horizons[72].price is not None else "",
            horizons[72].return_pct if horizons[72].return_pct is not None else "",
            horizons[168].price if horizons[168].price is not None else "",
            horizons[168].return_pct if horizons[168].return_pct is not None else "",
            breaches[50].occurred_at.isoformat() if breaches[50].occurred_at else "",
            breaches[50].hours_after_signal if breaches[50].hours_after_signal is not None else "",
            breaches[100].occurred_at.isoformat() if breaches[100].occurred_at else "",
            breaches[100].hours_after_signal if breaches[100].hours_after_signal is not None else "",
            breaches[200].occurred_at.isoformat() if breaches[200].occurred_at else "",
            breaches[200].hours_after_signal if breaches[200].hours_after_signal is not None else "",
            breaches[300].occurred_at.isoformat() if breaches[300].occurred_at else "",
            breaches[300].hours_after_signal if breaches[300].hours_after_signal is not None else "",
            breaches[400].occurred_at.isoformat() if breaches[400].occurred_at else "",
            breaches[400].hours_after_signal if breaches[400].hours_after_signal is not None else "",
        ])
    return output.getvalue().encode("utf-8")
