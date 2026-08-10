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
class PerformanceSummary:
    report_date: date
    confirmed_today: int
    open_count: int
    open_avg_return: float | None
    open_sum_return: float | None
    matured_total: int
    matured_today: int
    win_rate_24h: float | None
    standard_matured_total: int
    standard_win_rate_24h: float | None
    high_risk_matured_total: int
    high_risk_win_rate_24h: float | None
    avg_return_1h: float | None
    avg_return_4h: float | None
    avg_return_12h: float | None
    avg_return_24h: float | None
    sum_return_24h: float | None
    avg_mfe: float | None
    avg_mae: float | None
    best_symbol: str | None
    best_return_24h: float | None
    worst_symbol: str | None
    worst_return_24h: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date.isoformat(),
            "confirmed_today": self.confirmed_today,
            "open_count": self.open_count,
            "open_avg_return": self.open_avg_return,
            "open_sum_return": self.open_sum_return,
            "matured_total": self.matured_total,
            "matured_today": self.matured_today,
            "win_rate_24h": self.win_rate_24h,
            "standard_matured_total": self.standard_matured_total,
            "standard_win_rate_24h": self.standard_win_rate_24h,
            "high_risk_matured_total": self.high_risk_matured_total,
            "high_risk_win_rate_24h": self.high_risk_win_rate_24h,
            "avg_return_1h": self.avg_return_1h,
            "avg_return_4h": self.avg_return_4h,
            "avg_return_12h": self.avg_return_12h,
            "avg_return_24h": self.avg_return_24h,
            "sum_return_24h": self.sum_return_24h,
            "avg_mfe": self.avg_mfe,
            "avg_mae": self.avg_mae,
            "best_symbol": self.best_symbol,
            "best_return_24h": self.best_return_24h,
            "worst_symbol": self.worst_symbol,
            "worst_return_24h": self.worst_return_24h,
        }


def build_performance_summary(
    rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
    timezone_name: str,
) -> PerformanceSummary:
    """Build the current shadow-performance snapshot for a local calendar day.

    This is shared by the scheduled daily report and the manual Render-shell
    report so both use exactly the same calculations.
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
    open_rows = [row for row in rows if row["return_24h_pct"] is None]
    open_returns = [
        float(row["current_return_pct"])
        for row in open_rows
        if row["current_return_pct"] is not None
    ]
    matured = [row for row in rows if row["return_24h_pct"] is not None]
    matured_today = sum(
        1
        for row in matured
        if row["matured_at"] is not None
        and start_utc <= row["matured_at"] < end_utc
    )

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def values_for(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row[key] is not None]

    returns_24h = [float(row["return_24h_pct"]) for row in matured]
    wins = sum(value > 0 for value in returns_24h)
    standard_matured = [row for row in matured if row.get("risk_tier") == "standard"]
    high_risk_matured = [row for row in matured if row.get("risk_tier") != "standard"]

    def win_rate(group: list[dict[str, Any]]) -> float | None:
        values = [float(row["return_24h_pct"]) for row in group]
        if not values:
            return None
        return sum(value > 0 for value in values) / len(values)

    best = max(matured, key=lambda row: float(row["return_24h_pct"]), default=None)
    worst = min(matured, key=lambda row: float(row["return_24h_pct"]), default=None)
    matured_mfe = [float(row["mfe_pct"]) for row in matured if row["mfe_pct"] is not None]
    matured_mae = [float(row["mae_pct"]) for row in matured if row["mae_pct"] is not None]

    return PerformanceSummary(
        report_date=report_date,
        confirmed_today=confirmed_today,
        open_count=len(open_rows),
        open_avg_return=average(open_returns),
        open_sum_return=sum(open_returns) if open_returns else None,
        matured_total=len(matured),
        matured_today=matured_today,
        win_rate_24h=(wins / len(returns_24h)) if returns_24h else None,
        standard_matured_total=len(standard_matured),
        standard_win_rate_24h=win_rate(standard_matured),
        high_risk_matured_total=len(high_risk_matured),
        high_risk_win_rate_24h=win_rate(high_risk_matured),
        avg_return_1h=average(values_for("return_1h_pct")),
        avg_return_4h=average(values_for("return_4h_pct")),
        avg_return_12h=average(values_for("return_12h_pct")),
        avg_return_24h=average(returns_24h),
        sum_return_24h=sum(returns_24h) if returns_24h else None,
        avg_mfe=average(matured_mfe),
        avg_mae=average(matured_mae),
        best_symbol=str(best["symbol"]) if best is not None else None,
        best_return_24h=(float(best["return_24h_pct"]) if best is not None else None),
        worst_symbol=str(worst["symbol"]) if worst is not None else None,
        worst_return_24h=(float(worst["return_24h_pct"]) if worst is not None else None),
    )
