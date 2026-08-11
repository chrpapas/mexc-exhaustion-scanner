from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


def short_return(entry_price: float, exit_price: float) -> float:
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("prices must be positive")
    return (entry_price - exit_price) / entry_price


def should_send_daily_report(
    now_utc: datetime,
    *,
    timezone_name: str,
    report_hour: int,
    already_sent_date: date | None,
) -> bool:
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    if local_now.hour < report_hour:
        return False
    return already_sent_date != local_now.date()


@dataclass(frozen=True, slots=True)
class HorizonSummary:
    hours: int
    matured_total: int
    matured_today: int
    win_rate: float | None
    standard_total: int
    standard_win_rate: float | None
    standard_avg_return: float | None
    standard_sum_return: float | None
    high_risk_total: int
    high_risk_win_rate: float | None
    high_risk_avg_return: float | None
    high_risk_sum_return: float | None
    avg_return: float | None
    sum_return: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "matured_total": self.matured_total,
            "matured_today": self.matured_today,
            "win_rate": self.win_rate,
            "standard_total": self.standard_total,
            "standard_win_rate": self.standard_win_rate,
            "standard_avg_return": self.standard_avg_return,
            "standard_sum_return": self.standard_sum_return,
            "high_risk_total": self.high_risk_total,
            "high_risk_win_rate": self.high_risk_win_rate,
            "high_risk_avg_return": self.high_risk_avg_return,
            "high_risk_sum_return": self.high_risk_sum_return,
            "avg_return": self.avg_return,
            "sum_return": self.sum_return,
        }


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    report_date: date
    confirmed_today: int
    open_count: int
    open_avg_return: float | None
    open_sum_return: float | None
    horizon_24h: HorizonSummary
    horizon_48h: HorizonSummary
    horizon_72h: HorizonSummary
    avg_return_1h: float | None
    avg_return_4h: float | None
    avg_return_12h: float | None
    avg_mfe_72h: float | None
    avg_mae_72h: float | None
    best_symbol_72h: str | None
    best_return_72h: float | None
    worst_symbol_72h: str | None
    worst_return_72h: float | None

    # Compatibility properties retained for logs/tests/code that referred to
    # the original 24-hour-only summary.
    @property
    def matured_total(self) -> int:
        return self.horizon_24h.matured_total

    @property
    def matured_today(self) -> int:
        return self.horizon_24h.matured_today

    @property
    def win_rate_24h(self) -> float | None:
        return self.horizon_24h.win_rate

    @property
    def standard_matured_total(self) -> int:
        return self.horizon_24h.standard_total

    @property
    def standard_win_rate_24h(self) -> float | None:
        return self.horizon_24h.standard_win_rate

    @property
    def high_risk_matured_total(self) -> int:
        return self.horizon_24h.high_risk_total

    @property
    def high_risk_win_rate_24h(self) -> float | None:
        return self.horizon_24h.high_risk_win_rate

    @property
    def avg_return_24h(self) -> float | None:
        return self.horizon_24h.avg_return

    @property
    def sum_return_24h(self) -> float | None:
        return self.horizon_24h.sum_return

    @property
    def avg_return_48h(self) -> float | None:
        return self.horizon_48h.avg_return

    @property
    def avg_return_72h(self) -> float | None:
        return self.horizon_72h.avg_return

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date.isoformat(),
            "confirmed_today": self.confirmed_today,
            "open_count": self.open_count,
            "open_avg_return": self.open_avg_return,
            "open_sum_return": self.open_sum_return,
            "horizon_24h": self.horizon_24h.as_dict(),
            "horizon_48h": self.horizon_48h.as_dict(),
            "horizon_72h": self.horizon_72h.as_dict(),
            "avg_return_1h": self.avg_return_1h,
            "avg_return_4h": self.avg_return_4h,
            "avg_return_12h": self.avg_return_12h,
            "avg_mfe_72h": self.avg_mfe_72h,
            "avg_mae_72h": self.avg_mae_72h,
            "best_symbol_72h": self.best_symbol_72h,
            "best_return_72h": self.best_return_72h,
            "worst_symbol_72h": self.worst_symbol_72h,
            "worst_return_72h": self.worst_return_72h,
        }


def build_performance_summary(
    rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
    timezone_name: str,
) -> PerformanceSummary:
    """Build a 72-hour shadow-performance snapshot.

    Fixed horizons remain anchored to the CONFIRMED SHORT retest close. A trade
    is considered "open tracked" until its 72-hour return has been captured.
    """
    tz = ZoneInfo(timezone_name)
    local_now = now_utc.astimezone(tz)
    report_date = local_now.date()
    local_start = datetime(
        report_date.year, report_date.month, report_date.day, tzinfo=tz
    )
    next_date = report_date.fromordinal(report_date.toordinal() + 1)
    local_end = datetime(
        next_date.year, next_date.month, next_date.day, tzinfo=tz
    )
    start_utc = local_start.astimezone(ZoneInfo("UTC"))
    end_utc = local_end.astimezone(ZoneInfo("UTC"))

    confirmed_today = sum(
        1 for row in rows if start_utc <= row["confirmed_at"] < end_utc
    )
    open_rows = [row for row in rows if row.get("return_72h_pct") is None]
    open_returns = [
        float(row["current_return_pct"])
        for row in open_rows
        if row.get("current_return_pct") is not None
    ]

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def values_for(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    def horizon_summary(hours: int) -> HorizonSummary:
        return_key = f"return_{hours}h_pct"
        matured_key = "matured_at" if hours == 24 else f"matured_{hours}h_at"
        matured = [row for row in rows if row.get(return_key) is not None]
        matured_today = sum(
            1
            for row in matured
            if row.get(matured_key) is not None
            and start_utc <= row[matured_key] < end_utc
        )
        values = [float(row[return_key]) for row in matured]
        standard = [row for row in matured if row.get("risk_tier") == "standard"]
        high_risk = [row for row in matured if row.get("risk_tier") != "standard"]

        def win_rate(group: list[dict[str, Any]]) -> float | None:
            group_values = [float(row[return_key]) for row in group]
            if not group_values:
                return None
            return sum(value > 0 for value in group_values) / len(group_values)

        standard_values = [float(row[return_key]) for row in standard]
        high_risk_values = [float(row[return_key]) for row in high_risk]

        return HorizonSummary(
            hours=hours,
            matured_total=len(matured),
            matured_today=matured_today,
            win_rate=(sum(value > 0 for value in values) / len(values)) if values else None,
            standard_total=len(standard),
            standard_win_rate=win_rate(standard),
            standard_avg_return=average(standard_values),
            standard_sum_return=sum(standard_values) if standard_values else None,
            high_risk_total=len(high_risk),
            high_risk_win_rate=win_rate(high_risk),
            high_risk_avg_return=average(high_risk_values),
            high_risk_sum_return=sum(high_risk_values) if high_risk_values else None,
            avg_return=average(values),
            sum_return=sum(values) if values else None,
        )

    h24 = horizon_summary(24)
    h48 = horizon_summary(48)
    h72 = horizon_summary(72)

    matured_72 = [row for row in rows if row.get("return_72h_pct") is not None]
    mfe_72 = [float(row["mfe_pct"]) for row in matured_72 if row.get("mfe_pct") is not None]
    mae_72 = [float(row["mae_pct"]) for row in matured_72 if row.get("mae_pct") is not None]
    best_72 = max(matured_72, key=lambda row: float(row["return_72h_pct"]), default=None)
    worst_72 = min(matured_72, key=lambda row: float(row["return_72h_pct"]), default=None)

    return PerformanceSummary(
        report_date=report_date,
        confirmed_today=confirmed_today,
        open_count=len(open_rows),
        open_avg_return=average(open_returns),
        open_sum_return=sum(open_returns) if open_returns else None,
        horizon_24h=h24,
        horizon_48h=h48,
        horizon_72h=h72,
        avg_return_1h=average(values_for("return_1h_pct")),
        avg_return_4h=average(values_for("return_4h_pct")),
        avg_return_12h=average(values_for("return_12h_pct")),
        avg_mfe_72h=average(mfe_72),
        avg_mae_72h=average(mae_72),
        best_symbol_72h=str(best_72["symbol"]) if best_72 is not None else None,
        best_return_72h=(float(best_72["return_72h_pct"]) if best_72 is not None else None),
        worst_symbol_72h=str(worst_72["symbol"]) if worst_72 is not None else None,
        worst_return_72h=(float(worst_72["return_72h_pct"]) if worst_72 is not None else None),
    )
