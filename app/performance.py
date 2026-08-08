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
