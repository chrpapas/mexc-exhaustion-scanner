from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

PUBLIC_LEDGER_RISK_TIERS = frozenset({"standard", "high_risk"})


HORIZONS: tuple[tuple[int, str], ...] = (
    (24, "1D"),
    (48, "2D"),
    (72, "3D"),
    (168, "7D"),
)

BREACHES: tuple[tuple[int, str], ...] = (
    (100, "isolated_100_breach_at"),
    (200, "adverse_200_breach_at"),
    (300, "adverse_300_breach_at"),
    (400, "cross_400_breach_at"),
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
class SignalLedgerItem:
    episode_id: int
    symbol: str
    risk_tier: str
    confirmed_at: datetime
    signal_price: float
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


def build_signal_ledger(rows: Iterable[dict[str, Any]], *, generated_at: datetime) -> SignalLedger:
    items: list[SignalLedgerItem] = []
    for row in rows:
        risk_tier = str(row.get("risk_tier") or "standard")
        if risk_tier not in PUBLIC_LEDGER_RISK_TIERS:
            continue
        entry = float(row["entry_price"])
        confirmed_at = row["confirmed_at"]
        target_at = row.get("target_20_at")

        horizons: list[LedgerHorizon] = []
        for hours, label in HORIZONS:
            value = row.get(f"return_{hours}h_pct")
            ret = float(value) if value is not None else None
            horizons.append(LedgerHorizon(hours, label, ret, _horizon_price(entry, ret)))

        breaches: list[LedgerBreach] = []
        for threshold, key in BREACHES:
            occurred_at = row.get(key)
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


def signal_ledger_csv(ledger: SignalLedger) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "episode_id",
        "symbol",
        "risk_tier",
        "signal_time_utc",
        "signal_price",
        "headline_status",
        "target_20_at_utc",
        "time_to_target_20_hours",
        "current_return_pct",
        "price_1d",
        "return_1d_pct",
        "price_2d",
        "return_2d_pct",
        "price_3d",
        "return_3d_pct",
        "price_7d",
        "return_7d_pct",
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
        writer.writerow([
            item.episode_id,
            item.symbol,
            item.risk_tier,
            item.confirmed_at.isoformat(),
            item.signal_price,
            item.headline_status,
            item.target_20_at.isoformat() if item.target_20_at else "",
            item.time_to_target_20_hours if item.time_to_target_20_hours is not None else "",
            item.current_return_pct if item.current_return_pct is not None else "",
            horizons[24].price if horizons[24].price is not None else "",
            horizons[24].return_pct if horizons[24].return_pct is not None else "",
            horizons[48].price if horizons[48].price is not None else "",
            horizons[48].return_pct if horizons[48].return_pct is not None else "",
            horizons[72].price if horizons[72].price is not None else "",
            horizons[72].return_pct if horizons[72].return_pct is not None else "",
            horizons[168].price if horizons[168].price is not None else "",
            horizons[168].return_pct if horizons[168].return_pct is not None else "",
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
