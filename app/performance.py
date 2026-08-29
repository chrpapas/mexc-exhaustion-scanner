from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PUBLIC_PERFORMANCE_RISK_TIERS = frozenset({"standard", "high_risk"})
SHADOW_FEE_PER_FILL = 0.0008
MONTHLY_RUN_RATE_DAYS = 30.0

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
        return self.__dict__ if hasattr(self, "__dict__") else {
            name: getattr(self, name) for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class SurvivalModelSummary:
    survived: int
    survival_rate: float | None
    win_rate: float | None
    avg_return: float | None
    sum_return: float | None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ProfitTargetModelSummary:
    total_signals: int
    resolved: int
    wins: int
    breaches_before_target: int
    pending: int
    win_rate: float | None
    avg_time_to_target_hours: float | None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ProfitTargetSummary:
    risk_label: str
    isolated: ProfitTargetModelSummary
    cross_buffer: ProfitTargetModelSummary

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_label": self.risk_label,
            "isolated": self.isolated.as_dict(),
            "cross_buffer": self.cross_buffer.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class StrategyThresholdSummary:
    adverse_limit_pct: int
    total: int
    resolved: int
    wins: int
    failures: int
    pending: int
    win_rate: float | None
    avg_profit: float | None
    sum_profit: float | None
    avg_time_to_target_hours: float | None = None
    breach_failures: int = 0
    maturity_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class StrategyRowSummary:
    strategy: str
    label: str
    horizon_hours: int | None
    thresholds: tuple[StrategyThresholdSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "horizon_hours": self.horizon_hours,
            "thresholds": [item.as_dict() for item in self.thresholds],
        }


@dataclass(frozen=True, slots=True)
class StrategyMatrixSummary:
    risk_label: str
    total_signals: int
    rows: tuple[StrategyRowSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_label": self.risk_label,
            "total_signals": self.total_signals,
            "rows": [item.as_dict() for item in self.rows],
        }


@dataclass(frozen=True, slots=True)
class HorizonSurvivalSummary:
    hours: int
    risk_label: str
    matured_total: int
    isolated: SurvivalModelSummary
    cross_buffer: SurvivalModelSummary

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "risk_label": self.risk_label,
            "matured_total": self.matured_total,
            "isolated": self.isolated.as_dict(),
            "cross_buffer": self.cross_buffer.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class WeeklyRiskSummary:
    risk_label: str
    matured_7d: int
    ever_profitable_rate: float | None
    isolated_100_breach_rate: float | None
    cross_400_breach_rate: float | None
    isolated_breach_before_profit_rate: float | None
    cross_breach_before_profit_rate: float | None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class TP5PublicSummary:
    matured_7d: int
    hits_7d: int
    hit_rate_7d: float | None
    median_time_hours: float | None
    p75_time_hours: float | None
    median_pre_hit_adverse: float | None
    no_tp5_by_7d: int = 0
    avg_gross_captured_return: float | None = None
    sum_gross_captured_return: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class HighRiskTp20PublicSummary:
    sample: int
    target_hits: int
    target_hit_rate: float | None
    wins: int
    losses: int
    positive_rate: float | None
    avg_return: float | None
    median_return: float | None
    sum_return: float | None
    best_return: float | None
    worst_return: float | None
    avg_holding_hours: float | None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class Standard7dPublicSummary:
    matured_7d: int
    positive_rate: float | None
    avg_return: float | None
    median_return: float | None
    wins_7d: int = 0
    losses_7d: int = 0
    sum_return: float | None = None
    best_return: float | None = None
    worst_return: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class Normalized7dStrategySummary:
    """Equal-horizon strategy mark used for subscriber comparisons.

    Every included signal is valued exactly 168 hours after confirmation.
    Target strategies lock their target return when it is reached before the
    168h mark; otherwise the raw 168h signal return is used as mark-to-market.
    This is a comparison convention only and does not force a live timeout.
    """

    sample: int
    target_hits: int
    unresolved_at_7d: int
    wins: int
    losses: int
    positive_rate: float | None
    avg_return: float | None
    median_return: float | None
    sum_return: float | None
    best_return: float | None
    worst_return: float | None
    avg_effective_holding_hours: float | None
    median_effective_holding_hours: float | None
    breach_50: int
    breach_100: int
    breach_200: int
    breach_300: int
    worst_adverse: float | None

    @property
    def target_hit_rate(self) -> float | None:
        return (self.target_hits / self.sample) if self.sample else None

    def as_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["target_hit_rate"] = self.target_hit_rate
        return data


@dataclass(frozen=True, slots=True)
class ExposureRecommendation:
    per_trade_pct: float
    max_slots: int
    max_account_exposure_pct: float
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AccountRunRateSummary:
    """Chronological account replay under one subscriber strategy configuration.

    Returns include the configured shadow fill fee on entry and on realized/marked
    exit. Open positions are marked to the report timestamp. The 30-day figure is
    a linear equivalent run-rate from the observed calendar span, not a forecast
    or a claim of an observed 30-day result.
    """

    strategy: str
    start_at: datetime | None
    end_at: datetime
    span_days: float
    eligible_signals: int
    entered: int
    closed: int
    open_positions: int
    unmarked_open_positions: int
    missed_capacity: int
    missed_same_symbol: int
    observed_account_return: float | None
    thirty_day_equivalent_return: float | None
    thirty_day_pnl_per_10k: float | None
    avg_exposure_pct: float | None
    peak_exposure_pct: float | None
    slot_days: float
    max_mtm_drawdown: float | None = None
    return_over_max_drawdown: float | None = None
    fee_per_fill: float = SHADOW_FEE_PER_FILL

    def as_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["start_at"] = self.start_at.isoformat() if self.start_at else None
        data["end_at"] = self.end_at.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class TraderStrategySummary:
    strategy: str
    label: str
    rule: str
    sample: int
    target_exits: int
    stop_exits: int
    timeout_exits: int
    waiting: int
    marked_sample: int
    marked_positive_rate: float | None
    avg_marked_return: float | None
    median_marked_return: float | None
    sum_marked_return: float | None
    positive_exits: int
    negative_exits: int
    resolved_positive_rate: float | None
    avg_exit_return: float | None
    median_exit_return: float | None
    worst_exit_return: float | None
    median_holding_hours: float | None
    p75_holding_hours: float | None
    breach_50: int
    breach_75: int
    breach_100: int

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


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
    horizon_168h: HorizonSummary
    standard_survival: tuple[HorizonSurvivalSummary, ...]
    risky_survival: tuple[HorizonSurvivalSummary, ...]
    standard_weekly: WeeklyRiskSummary
    risky_weekly: WeeklyRiskSummary
    high_weekly: WeeklyRiskSummary
    extreme_weekly: WeeklyRiskSummary
    standard_profit_target: ProfitTargetSummary
    risky_profit_target: ProfitTargetSummary
    standard_strategy_matrix: StrategyMatrixSummary
    risky_strategy_matrix: StrategyMatrixSummary
    high_strategy_matrix: StrategyMatrixSummary
    extreme_strategy_matrix: StrategyMatrixSummary
    avg_return_1h: float | None
    avg_return_4h: float | None
    avg_return_12h: float | None
    avg_mfe_7d: float | None
    avg_mae_7d: float | None
    best_symbol_7d: str | None
    best_return_7d: float | None
    worst_symbol_7d: str | None
    worst_return_7d: float | None
    tp5_public: TP5PublicSummary = TP5PublicSummary(0, 0, None, None, None, None)
    high_risk_tp20_public: HighRiskTp20PublicSummary = HighRiskTp20PublicSummary(0, 0, None, 0, 0, None, None, None, None, None, None, None)
    standard_7d_public: Standard7dPublicSummary = Standard7dPublicSummary(0, None, None, None)
    comparison_start_at: datetime | None = None
    comparison_end_at: datetime | None = None
    tp5_7d_comparison: Normalized7dStrategySummary | None = None
    tp20_7d_comparison: Normalized7dStrategySummary | None = None
    standard_7d_comparison: Normalized7dStrategySummary | None = None
    trader_strategy_tp5: TraderStrategySummary | None = None
    trader_strategy_tp5_sl75: TraderStrategySummary | None = None
    trader_strategy_hold_7d: TraderStrategySummary | None = None
    tp5_exposure: ExposureRecommendation = ExposureRecommendation(0.05, 6, 0.30, "portfolio-tested frozen TP5 configuration")
    tp20_exposure: ExposureRecommendation = ExposureRecommendation(0.02, 5, 0.10, "risk-based suggestion from observed HIGH_RISK adverse paths")
    standard_7d_exposure: ExposureRecommendation = ExposureRecommendation(0.03, 5, 0.15, "risk-based suggestion for fixed 7-day STANDARD holds")
    tp5_account_run_rate: AccountRunRateSummary | None = None
    tp5_sl75_account_run_rate: AccountRunRateSummary | None = None
    hold_7d_account_run_rate: AccountRunRateSummary | None = None
    tp20_account_run_rate: AccountRunRateSummary | None = None
    standard_7d_account_run_rate: AccountRunRateSummary | None = None

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
    def avg_return_24h(self) -> float | None:
        return self.horizon_24h.avg_return

    @property
    def avg_return_48h(self) -> float | None:
        return self.horizon_48h.avg_return

    @property
    def avg_return_72h(self) -> float | None:
        return self.horizon_72h.avg_return

    @property
    def avg_return_168h(self) -> float | None:
        return self.horizon_168h.avg_return

    @property
    def avg_mfe_72h(self) -> float | None:  # backwards compatibility
        return self.avg_mfe_7d

    @property
    def avg_mae_72h(self) -> float | None:
        return self.avg_mae_7d

    @property
    def best_symbol_72h(self) -> str | None:
        return self.best_symbol_7d

    @property
    def best_return_72h(self) -> float | None:
        return self.best_return_7d

    @property
    def worst_symbol_72h(self) -> str | None:
        return self.worst_symbol_7d

    @property
    def worst_return_72h(self) -> float | None:
        return self.worst_return_7d

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
            "horizon_168h": self.horizon_168h.as_dict(),
            "standard_survival": [x.as_dict() for x in self.standard_survival],
            "risky_survival": [x.as_dict() for x in self.risky_survival],
            "standard_weekly": self.standard_weekly.as_dict(),
            "risky_weekly": self.risky_weekly.as_dict(),
            "high_weekly": self.high_weekly.as_dict(),
            "extreme_weekly": self.extreme_weekly.as_dict(),
            "standard_profit_target": self.standard_profit_target.as_dict(),
            "risky_profit_target": self.risky_profit_target.as_dict(),
            "standard_strategy_matrix": self.standard_strategy_matrix.as_dict(),
            "risky_strategy_matrix": self.risky_strategy_matrix.as_dict(),
            "high_strategy_matrix": self.high_strategy_matrix.as_dict(),
            "extreme_strategy_matrix": self.extreme_strategy_matrix.as_dict(),
            "avg_return_1h": self.avg_return_1h,
            "avg_return_4h": self.avg_return_4h,
            "avg_return_12h": self.avg_return_12h,
            "avg_mfe_7d": self.avg_mfe_7d,
            "avg_mae_7d": self.avg_mae_7d,
            "best_symbol_7d": self.best_symbol_7d,
            "best_return_7d": self.best_return_7d,
            "worst_symbol_7d": self.worst_symbol_7d,
            "worst_return_7d": self.worst_return_7d,
            "tp5_public": self.tp5_public.as_dict(),
            "high_risk_tp20_public": self.high_risk_tp20_public.as_dict(),
            "standard_7d_public": self.standard_7d_public.as_dict(),
            "comparison_start_at": self.comparison_start_at.isoformat() if self.comparison_start_at else None,
            "comparison_end_at": self.comparison_end_at.isoformat() if self.comparison_end_at else None,
            "tp5_7d_comparison": self.tp5_7d_comparison.as_dict() if self.tp5_7d_comparison else None,
            "tp20_7d_comparison": self.tp20_7d_comparison.as_dict() if self.tp20_7d_comparison else None,
            "standard_7d_comparison": self.standard_7d_comparison.as_dict() if self.standard_7d_comparison else None,
            "trader_strategy_tp5": self.trader_strategy_tp5.as_dict() if self.trader_strategy_tp5 else None,
            "trader_strategy_tp5_sl75": self.trader_strategy_tp5_sl75.as_dict() if self.trader_strategy_tp5_sl75 else None,
            "trader_strategy_hold_7d": self.trader_strategy_hold_7d.as_dict() if self.trader_strategy_hold_7d else None,
            "tp5_exposure": self.tp5_exposure.as_dict(),
            "tp20_exposure": self.tp20_exposure.as_dict(),
            "standard_7d_exposure": self.standard_7d_exposure.as_dict(),
            "tp5_account_run_rate": self.tp5_account_run_rate.as_dict() if self.tp5_account_run_rate else None,
            "tp5_sl75_account_run_rate": self.tp5_sl75_account_run_rate.as_dict() if self.tp5_sl75_account_run_rate else None,
            "hold_7d_account_run_rate": self.hold_7d_account_run_rate.as_dict() if self.hold_7d_account_run_rate else None,
            "tp20_account_run_rate": self.tp20_account_run_rate.as_dict() if self.tp20_account_run_rate else None,
            "standard_7d_account_run_rate": self.standard_7d_account_run_rate.as_dict() if self.standard_7d_account_run_rate else None,
        }


def build_performance_summary(
    rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
    timezone_name: str,
) -> PerformanceSummary:
    tz = ZoneInfo(timezone_name)
    local_now = now_utc.astimezone(tz)
    report_date = local_now.date()
    local_start = datetime(report_date.year, report_date.month, report_date.day, tzinfo=tz)
    next_date = report_date.fromordinal(report_date.toordinal() + 1)
    local_end = datetime(next_date.year, next_date.month, next_date.day, tzinfo=tz)
    utc = ZoneInfo("UTC")
    start_utc = local_start.astimezone(utc)
    end_utc = local_end.astimezone(utc)

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def rate(flags: list[bool]) -> float | None:
        return sum(flags) / len(flags) if flags else None

    def percentile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        pos = (len(ordered) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(ordered) - 1)
        frac = pos - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

    # Subscriber-facing performance deliberately excludes EXTREME risk.
    # Extreme signals remain stored by the scanner for internal research, but
    # they do not contribute to public counts, returns, excursions, or ledgers.
    rows = [
        row for row in rows
        if str(row.get("risk_tier") or "standard") in PUBLIC_PERFORMANCE_RISK_TIERS
    ]

    confirmed_today = sum(1 for row in rows if start_utc <= row["confirmed_at"] < end_utc)
    open_rows = [row for row in rows if row.get("return_168h_pct") is None]
    open_returns = [float(row["current_return_pct"]) for row in open_rows if row.get("current_return_pct") is not None]

    def values_for(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    def horizon_summary(hours: int) -> HorizonSummary:
        return_key = f"return_{hours}h_pct"
        matured_key = "matured_at" if hours == 24 else f"matured_{hours}h_at"
        matured = [row for row in rows if row.get(return_key) is not None]
        matured_today = sum(
            1 for row in matured
            if row.get(matured_key) is not None and start_utc <= row[matured_key] < end_utc
        )
        values = [float(row[return_key]) for row in matured]
        standard = [row for row in matured if row.get("risk_tier") == "standard"]
        risky = [row for row in matured if row.get("risk_tier") != "standard"]

        def group_stats(group: list[dict[str, Any]]) -> tuple[float | None, float | None, float | None]:
            vals = [float(row[return_key]) for row in group]
            return rate([v > 0 for v in vals]), average(vals), (sum(vals) if vals else None)

        sw, sa, ss = group_stats(standard)
        rw, ra, rs = group_stats(risky)
        return HorizonSummary(
            hours=hours,
            matured_total=len(matured),
            matured_today=matured_today,
            win_rate=rate([v > 0 for v in values]),
            standard_total=len(standard),
            standard_win_rate=sw,
            standard_avg_return=sa,
            standard_sum_return=ss,
            high_risk_total=len(risky),
            high_risk_win_rate=rw,
            high_risk_avg_return=ra,
            high_risk_sum_return=rs,
            avg_return=average(values),
            sum_return=sum(values) if values else None,
        )

    def horizon_survival(hours: int, *, standard: bool) -> HorizonSurvivalSummary:
        key = f"return_{hours}h_pct"
        group = [
            row for row in rows
            if row.get(key) is not None
            and ((row.get("risk_tier") == "standard") == standard)
        ]

        def model(event_key: str) -> SurvivalModelSummary:
            survivors: list[dict[str, Any]] = []
            for row in group:
                deadline = row["confirmed_at"] + timedelta(hours=hours)
                breach = row.get(event_key)
                if breach is None or breach > deadline:
                    survivors.append(row)

            vals = [float(row[key]) for row in survivors]
            return SurvivalModelSummary(
                survived=len(survivors),
                survival_rate=(len(survivors) / len(group)) if group else None,
                win_rate=rate([v > 0 for v in vals]),
                avg_return=average(vals),
                sum_return=sum(vals) if vals else None,
            )

        return HorizonSurvivalSummary(
            hours=hours,
            risk_label="STANDARD" if standard else "HIGH RISK",
            matured_total=len(group),
            isolated=model("isolated_100_breach_at"),
            cross_buffer=model("cross_400_breach_at"),
        )

    def profit_target_summary(*, standard: bool) -> ProfitTargetSummary:
        group = [
            row for row in rows
            if ((row.get("risk_tier") == "standard") == standard)
        ]

        def model(event_key: str) -> ProfitTargetModelSummary:
            wins = 0
            breaches = 0
            target_times: list[float] = []
            for row in group:
                target = row.get("target_20_at")
                breach = row.get(event_key)

                # The +20% race is deliberately independent of the 1d/2d/3d/7d
                # return horizons. A target-first signal is a win; a breach-first
                # signal is a loss for that liquidation proxy; unresolved signals
                # remain pending and are excluded from the resolved win-rate
                # denominator. Same-candle target/breach is conservatively breach-first.
                if target is not None and (breach is None or target < breach):
                    wins += 1
                    target_times.append(
                        (target - row["confirmed_at"]).total_seconds() / 3600.0
                    )
                elif breach is not None and (target is None or breach <= target):
                    breaches += 1

            resolved = wins + breaches
            pending = len(group) - resolved
            return ProfitTargetModelSummary(
                total_signals=len(group),
                resolved=resolved,
                wins=wins,
                breaches_before_target=breaches,
                pending=pending,
                win_rate=(wins / resolved) if resolved else None,
                avg_time_to_target_hours=average(target_times),
            )

        return ProfitTargetSummary(
            risk_label="STANDARD" if standard else "HIGH RISK",
            isolated=model("isolated_100_breach_at"),
            cross_buffer=model("cross_400_breach_at"),
        )

    def strategy_matrix_for(*, risk_label: str, risk_tiers: set[str]) -> StrategyMatrixSummary:
        group = [row for row in rows if str(row.get("risk_tier") or "") in risk_tiers]
        threshold_events = (
            (100, "isolated_100_breach_at"),
            (200, "adverse_200_breach_at"),
            (300, "adverse_300_breach_at"),
            (400, "cross_400_breach_at"),
        )

        target_cells: list[StrategyThresholdSummary] = []
        for adverse_limit, event_key in threshold_events:
            wins = 0
            failures = 0
            target_times: list[float] = []
            for row in group:
                target = row.get("target_20_at")
                breach = row.get(event_key)
                if target is not None and (breach is None or target < breach):
                    wins += 1
                    target_times.append((target - row["confirmed_at"]).total_seconds() / 3600.0)
                elif breach is not None and (target is None or breach <= target):
                    failures += 1
            resolved = wins + failures
            pending = len(group) - resolved
            wr = (wins / resolved) if resolved else None
            target_cells.append(StrategyThresholdSummary(
                adverse_limit_pct=adverse_limit,
                total=len(group),
                resolved=resolved,
                wins=wins,
                failures=failures,
                pending=pending,
                win_rate=wr,
                avg_profit=None,
                sum_profit=None,
                avg_time_to_target_hours=average(target_times),
            ))

        strategy_rows: list[StrategyRowSummary] = [StrategyRowSummary(
            strategy="profit_20",
            label="+20% target",
            horizon_hours=None,
            thresholds=tuple(target_cells),
        )]

        for hours, label in ((24, "1D outcomes"), (48, "2D outcomes"), (72, "3D outcomes"), (168, "7D outcomes")):
            return_key = f"return_{hours}h_pct"
            matured = [row for row in group if row.get(return_key) is not None]
            deadline_delta = timedelta(hours=hours)
            raw_returns = [float(row[return_key]) for row in matured]
            profitable = sum(value > 0 for value in raw_returns)
            not_profitable = len(raw_returns) - profitable
            raw_positive_rate = (profitable / len(raw_returns)) if raw_returns else None
            raw_avg = average(raw_returns)
            raw_sum = sum(raw_returns) if raw_returns else None

            # Fixed-horizon outcomes are raw observations, not stop-loss
            # strategies. Every matured return contributes to Avg and Σ even if
            # that signal crossed one or more adverse thresholds on the way. The
            # threshold counts are therefore independent path statistics and may
            # overlap with profitable/not-profitable outcomes.
            cells: list[StrategyThresholdSummary] = []
            for adverse_limit, event_key in threshold_events:
                breach_count = 0
                for row in matured:
                    deadline = row["confirmed_at"] + deadline_delta
                    breach = row.get(event_key)
                    if breach is not None and breach <= deadline:
                        breach_count += 1
                cells.append(StrategyThresholdSummary(
                    adverse_limit_pct=adverse_limit,
                    total=len(matured),
                    resolved=len(matured),
                    wins=profitable,
                    failures=not_profitable,
                    pending=0,
                    win_rate=raw_positive_rate,
                    avg_profit=raw_avg,
                    sum_profit=raw_sum,
                    breach_failures=breach_count,
                    maturity_failures=not_profitable,
                ))
            strategy_rows.append(StrategyRowSummary(
                strategy=f"{hours}h",
                label=label,
                horizon_hours=hours,
                thresholds=tuple(cells),
            ))

        return StrategyMatrixSummary(
            risk_label=risk_label,
            total_signals=len(group),
            rows=tuple(strategy_rows),
        )

    def strategy_matrix(*, standard: bool) -> StrategyMatrixSummary:
        return strategy_matrix_for(
            risk_label="STANDARD" if standard else "HIGH RISK",
            risk_tiers={"standard"} if standard else {"high_risk"},
        )

    def weekly_risk_for(*, risk_label: str, risk_tiers: set[str]) -> WeeklyRiskSummary:
        group = [
            row for row in rows
            if row.get("return_168h_pct") is not None
            and str(row.get("risk_tier") or "") in risk_tiers
        ]
        if not group:
            return WeeklyRiskSummary(
                risk_label=risk_label,
                matured_7d=0,
                ever_profitable_rate=None,
                isolated_100_breach_rate=None,
                cross_400_breach_rate=None,
                isolated_breach_before_profit_rate=None,
                cross_breach_before_profit_rate=None,
            )

        def within_week(row: dict[str, Any], key: str) -> datetime | None:
            value = row.get(key)
            if value is None:
                return None
            return value if value <= row["confirmed_at"] + timedelta(hours=168) else None

        first_profit = [within_week(row, "first_profit_at") for row in group]
        iso = [within_week(row, "isolated_100_breach_at") for row in group]
        cross = [within_week(row, "cross_400_breach_at") for row in group]

        return WeeklyRiskSummary(
            risk_label=risk_label,
            matured_7d=len(group),
            ever_profitable_rate=rate([x is not None for x in first_profit]),
            isolated_100_breach_rate=rate([x is not None for x in iso]),
            cross_400_breach_rate=rate([x is not None for x in cross]),
            isolated_breach_before_profit_rate=rate([
                i is not None and (p is None or i < p) for i, p in zip(iso, first_profit)
            ]),
            cross_breach_before_profit_rate=rate([
                c is not None and (p is None or c < p) for c, p in zip(cross, first_profit)
            ]),
        )

    def weekly_risk(*, standard: bool) -> WeeklyRiskSummary:
        return weekly_risk_for(
            risk_label="STANDARD" if standard else "HIGH RISK",
            risk_tiers={"standard"} if standard else {"high_risk"},
        )

    h24 = horizon_summary(24)
    h48 = horizon_summary(48)
    h72 = horizon_summary(72)
    h168 = horizon_summary(168)

    matured_7d = [row for row in rows if row.get("return_168h_pct") is not None]
    mfe = [float(row["mfe_pct"]) for row in matured_7d if row.get("mfe_pct") is not None]
    mae = [float(row["mae_pct"]) for row in matured_7d if row.get("mae_pct") is not None]
    best = max(matured_7d, key=lambda row: float(row["return_168h_pct"]), default=None)
    worst = min(matured_7d, key=lambda row: float(row["return_168h_pct"]), default=None)

    tp5_times: list[float] = []
    tp5_adverse: list[float] = []
    tp5_hits = 0
    for row in matured_7d:
        target = row.get("target_5_at")
        if target is None or target > row["confirmed_at"] + timedelta(hours=168):
            continue
        tp5_hits += 1
        tp5_times.append((target - row["confirmed_at"]).total_seconds() / 3600.0)
        adverse = row.get("path_mae_before_target_5")
        if adverse is not None:
            tp5_adverse.append(abs(float(adverse)))
    tp5_public = TP5PublicSummary(
        matured_7d=len(matured_7d),
        hits_7d=tp5_hits,
        hit_rate_7d=(tp5_hits / len(matured_7d)) if matured_7d else None,
        median_time_hours=percentile(tp5_times, 0.50),
        p75_time_hours=percentile(tp5_times, 0.75),
        median_pre_hit_adverse=percentile(tp5_adverse, 0.50),
        no_tp5_by_7d=len(matured_7d) - tp5_hits,
        avg_gross_captured_return=(tp5_hits * 0.05 / len(matured_7d)) if matured_7d else None,
        sum_gross_captured_return=(tp5_hits * 0.05) if matured_7d else None,
    )
    # Retained historical HIGH_RISK TP20-or-4D summary for internal/backward
    # compatibility. It is no longer a subscriber-recommended public strategy.
    # Keep the same strict paired 10-day research cohort used to identify the
    # 4-day timeout as the strongest observed HIGH_RISK variant. This avoids
    # changing the denominator as shorter horizons mature.
    high_paired_10d: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("risk_tier") or "standard") != "high_risk":
            continue
        confirmed = row.get("confirmed_at")
        path_last = row.get("path_last_at")
        path_rows_10d = row.get("path_rows_10d")
        if confirmed is None or path_last is None or path_rows_10d is None:
            continue
        if path_last < confirmed + timedelta(hours=240) - timedelta(minutes=15):
            continue
        if int(path_rows_10d) < 941:  # ceil(240h * 4 bars/h * 98%)
            continue
        paired_keys = (
            "path_return_24h", "path_return_48h", "path_return_72h",
            "path_return_96h", "path_return_120h", "path_return_168h",
            "path_return_240h",
        )
        if any(row.get(key) is None for key in paired_keys):
            continue
        high_paired_10d.append(row)

    high_outcomes: list[float] = []
    high_holding_hours: list[float] = []
    high_target_hits = 0
    for row in high_paired_10d:
        confirmed = row["confirmed_at"]
        candidates = [
            value for value in (row.get("target_20_path_at"), row.get("target_20_at"))
            if value is not None
        ]
        target_at = min(candidates) if candidates else None
        if target_at is not None and target_at <= confirmed + timedelta(hours=96):
            high_outcomes.append(0.20)
            high_holding_hours.append(
                min(96.0, (target_at - confirmed).total_seconds() / 3600.0)
            )
            high_target_hits += 1
        else:
            high_outcomes.append(float(row["path_return_96h"]))
            high_holding_hours.append(96.0)

    high_wins = sum(value > 0 for value in high_outcomes)
    high_risk_tp20_public = HighRiskTp20PublicSummary(
        sample=len(high_outcomes),
        target_hits=high_target_hits,
        target_hit_rate=(high_target_hits / len(high_outcomes)) if high_outcomes else None,
        wins=high_wins,
        losses=len(high_outcomes) - high_wins,
        positive_rate=rate([value > 0 for value in high_outcomes]),
        avg_return=average(high_outcomes),
        median_return=percentile(high_outcomes, 0.50),
        sum_return=sum(high_outcomes) if high_outcomes else None,
        best_return=max(high_outcomes) if high_outcomes else None,
        worst_return=min(high_outcomes) if high_outcomes else None,
        avg_holding_hours=average(high_holding_hours),
    )

    standard_7d_values = [
        float(row["return_168h_pct"])
        for row in matured_7d
        if str(row.get("risk_tier") or "standard") == "standard"
    ]
    standard_7d_wins = sum(value > 0 for value in standard_7d_values)
    standard_7d_public = Standard7dPublicSummary(
        matured_7d=len(standard_7d_values),
        positive_rate=rate([value > 0 for value in standard_7d_values]),
        avg_return=average(standard_7d_values),
        median_return=percentile(standard_7d_values, 0.50),
        wins_7d=standard_7d_wins,
        losses_7d=len(standard_7d_values) - standard_7d_wins,
        sum_return=sum(standard_7d_values) if standard_7d_values else None,
        best_return=max(standard_7d_values) if standard_7d_values else None,
        worst_return=min(standard_7d_values) if standard_7d_values else None,
    )

    # Subscriber strategy comparison: value every strategy on the exact same
    # 168-hour observation horizon. Target strategies lock the target if hit
    # before 7d; otherwise they remain open and are marked at the 7d price.
    # This creates comparable Σ/average/win-rate statistics without changing
    # the actual TP5 or open-ended TP20 exit rules.
    comparison_start_at = min(
        (row["confirmed_at"] for row in matured_7d),
        default=None,
    )
    comparison_end_at = max(
        (row["confirmed_at"] for row in matured_7d),
        default=None,
    )

    def earliest_event(row: dict[str, Any], *keys: str) -> datetime | None:
        values = [row.get(key) for key in keys if row.get(key) is not None]
        return min(values) if values else None

    def trader_strategy_summary(strategy: str) -> TraderStrategySummary:
        labels = {
            "tp5": ("TP5 indefinite", "+5% target • no stop • no timeout"),
            "tp5_sl75": ("TP5 + SL75", "+5% target • -75% catastrophic stop • no timeout"),
            "hold_7d": ("7D hold", "hold exactly 168h • close at the 7D return • no TP / no SL"),
        }
        label, rule = labels[strategy]
        statuses: list[str] = []
        exit_returns: list[float] = []
        marked_returns: list[float] = []
        holding_hours: list[float] = []
        breaches = {50: 0, 75: 0, 100: 0}
        observed = [
            row for row in rows
            if row.get("confirmed_at") is not None and row["confirmed_at"] <= now_utc
            and str(row.get("risk_tier") or "standard") in {"standard", "high_risk"}
        ]

        for row in observed:
            confirmed = row["confirmed_at"]
            target = earliest_event(row, "target_5_at")
            status = "waiting"
            exit_at = None
            exit_return = None
            if strategy == "tp5":
                if target is not None and target <= now_utc:
                    status, exit_at, exit_return = "target", target, 0.05
            elif strategy == "tp5_sl75":
                stop = earliest_event(row, "adverse_75_at")
                if stop is not None and stop <= now_utc and (target is None or stop <= target):
                    status, exit_at, exit_return = "stop", stop, -0.75
                elif target is not None and target <= now_utc:
                    status, exit_at, exit_return = "target", target, 0.05
            elif strategy == "hold_7d":
                cutoff = confirmed + timedelta(hours=168)
                if cutoff <= now_utc:
                    value = row.get("return_168h_pct")
                    if value is None:
                        value = row.get("path_return_168h")
                    if value is not None:
                        status, exit_at, exit_return = "timeout", cutoff, float(value)
            else:
                raise ValueError(f"unsupported trader strategy: {strategy}")

            statuses.append(status)
            effective_end = exit_at or now_utc
            for threshold, key in ((50, "adverse_50_at"), (75, "adverse_75_at"), (100, "adverse_100_at")):
                event = row.get(key)
                if event is not None and event <= effective_end:
                    breaches[threshold] += 1
            if exit_at is not None and exit_return is not None:
                exit_returns.append(float(exit_return))
                marked_returns.append(float(exit_return) - (2.0 * SHADOW_FEE_PER_FILL))
                holding_hours.append(max(0.0, (exit_at - confirmed).total_seconds() / 3600.0))
            else:
                mark = row.get("current_return_pct")
                if mark is None:
                    mark = row.get("path_latest_return")
                if mark is None:
                    for hours in (336, 288, 240, 192, 168, 144, 120, 96, 72, 48, 24):
                        candidate = row.get(f"path_return_{hours}h")
                        if candidate is not None:
                            mark = candidate
                            break
                if mark is not None:
                    marked_returns.append(float(mark) - (2.0 * SHADOW_FEE_PER_FILL))

        resolved = len(exit_returns)
        positive = sum(value > 0 for value in exit_returns)
        return TraderStrategySummary(
            strategy=strategy,
            label=label,
            rule=rule,
            sample=len(observed),
            target_exits=statuses.count("target"),
            stop_exits=statuses.count("stop"),
            timeout_exits=statuses.count("timeout"),
            waiting=statuses.count("waiting"),
            marked_sample=len(marked_returns),
            marked_positive_rate=rate([value > 0 for value in marked_returns]),
            avg_marked_return=average(marked_returns),
            median_marked_return=percentile(marked_returns, 0.50),
            sum_marked_return=sum(marked_returns) if marked_returns else None,
            positive_exits=positive,
            negative_exits=resolved - positive,
            resolved_positive_rate=(positive / resolved) if resolved else None,
            avg_exit_return=average(exit_returns),
            median_exit_return=percentile(exit_returns, 0.50),
            worst_exit_return=min(exit_returns) if exit_returns else None,
            median_holding_hours=percentile(holding_hours, 0.50),
            p75_holding_hours=percentile(holding_hours, 0.75),
            breach_50=breaches[50],
            breach_75=breaches[75],
            breach_100=breaches[100],
        )


    def normalized_7d_strategy(
        group: list[dict[str, Any]],
        *,
        target_return: float | None,
        target_keys: tuple[str, ...] = (),
        target_mae_key: str | None = None,
    ) -> Normalized7dStrategySummary:
        outcomes: list[float] = []
        holding_hours: list[float] = []
        adverse_magnitudes: list[float] = []
        target_hits = 0
        breaches = {50: 0, 100: 0, 200: 0, 300: 0}

        for row in group:
            confirmed = row["confirmed_at"]
            horizon_at = confirmed + timedelta(hours=168)
            target_at = earliest_event(row, *target_keys) if target_keys else None
            target_hit = (
                target_return is not None
                and target_at is not None
                and target_at <= horizon_at
            )
            if target_hit:
                target_hits += 1
                outcome = float(target_return)
                effective_exit = target_at
                if target_mae_key and row.get(target_mae_key) is not None:
                    adverse_magnitudes.append(abs(float(row[target_mae_key])))
            else:
                outcome = float(row["return_168h_pct"])
                effective_exit = horizon_at
                if row.get("path_mae_7d") is not None:
                    adverse_magnitudes.append(abs(float(row["path_mae_7d"])))

            outcomes.append(outcome)
            holding_hours.append(
                max(0.0, min(168.0, (effective_exit - confirmed).total_seconds() / 3600.0))
            )

            event_times = {
                50: earliest_event(row, "adverse_50_at"),
                100: earliest_event(row, "adverse_100_at", "isolated_100_breach_at"),
                200: earliest_event(row, "adverse_200_path_at", "adverse_200_breach_at"),
                300: earliest_event(row, "adverse_300_path_at", "adverse_300_breach_at"),
            }
            for threshold, event_at in event_times.items():
                # Count same-candle target/breach as a breach for conservative
                # subscriber risk reporting. Later breaches after an early target
                # exit are irrelevant to that strategy and are not counted.
                if event_at is not None and event_at <= effective_exit:
                    breaches[threshold] += 1

        wins = sum(value > 0 for value in outcomes)
        return Normalized7dStrategySummary(
            sample=len(outcomes),
            target_hits=target_hits,
            unresolved_at_7d=(len(outcomes) - target_hits) if target_return is not None else 0,
            wins=wins,
            losses=len(outcomes) - wins,
            positive_rate=rate([value > 0 for value in outcomes]),
            avg_return=average(outcomes),
            median_return=percentile(outcomes, 0.50),
            sum_return=sum(outcomes) if outcomes else None,
            best_return=max(outcomes) if outcomes else None,
            worst_return=min(outcomes) if outcomes else None,
            avg_effective_holding_hours=average(holding_hours),
            median_effective_holding_hours=percentile(holding_hours, 0.50),
            breach_50=breaches[50],
            breach_100=breaches[100],
            breach_200=breaches[200],
            breach_300=breaches[300],
            worst_adverse=max(adverse_magnitudes) if adverse_magnitudes else None,
        )

    standard_matured_7d = [
        row for row in matured_7d
        if str(row.get("risk_tier") or "standard") == "standard"
    ]
    high_matured_7d = [
        row for row in matured_7d
        if str(row.get("risk_tier") or "standard") == "high_risk"
    ]
    tp5_7d_comparison = normalized_7d_strategy(
        matured_7d,
        target_return=0.05,
        target_keys=("target_5_at",),
        target_mae_key="path_mae_before_target_5",
    )
    tp20_7d_comparison = normalized_7d_strategy(
        high_matured_7d,
        target_return=0.20,
        target_keys=("target_20_path_at", "target_20_at"),
        target_mae_key="path_mae_before_target_20",
    )
    standard_7d_comparison = normalized_7d_strategy(
        standard_matured_7d,
        target_return=None,
    )

    # Subscriber account replay. Unlike the 7D signal-normalization table above,
    # this follows each strategy's actual exit rule through the common report
    # timestamp and respects its suggested slot sizing/capacity. This is the
    # basis for the 30-day equivalent run-rate shown publicly.
    account_start_at = min(
        (row["confirmed_at"] for row in rows if row.get("confirmed_at") is not None and row["confirmed_at"] <= now_utc),
        default=None,
    )

    def account_run_rate(
        *,
        strategy: str,
        risk_tiers: set[str],
        exposure: ExposureRecommendation,
    ) -> AccountRunRateSummary:
        ordered = sorted(
            [row for row in rows if row.get("confirmed_at") is not None and row["confirmed_at"] <= now_utc],
            key=lambda row: row["confirmed_at"],
        )
        span_days = (
            max(0.0, (now_utc - account_start_at).total_seconds() / 86400.0)
            if account_start_at is not None else 0.0
        )
        equity = 1.0
        positions: list[dict[str, Any]] = []
        all_positions: list[dict[str, Any]] = []
        holds_hours: list[float] = []
        eligible_signals = entered = closed = missed_capacity = missed_same_symbol = 0
        max_open = 0

        def known_exit(row: dict[str, Any]) -> tuple[datetime | None, float | None]:
            confirmed = row["confirmed_at"]
            if strategy == "tp5":
                target = earliest_event(row, "target_5_at")
                if target is not None and target <= now_utc:
                    return target, 0.05
                return None, None
            if strategy == "tp5_sl75":
                target = earliest_event(row, "target_5_at")
                stop = earliest_event(row, "adverse_75_at")
                if stop is not None and stop <= now_utc and (target is None or stop <= target):
                    return stop, -0.75
                if target is not None and target <= now_utc:
                    return target, 0.05
                return None, None
            if strategy == "hold_7d":
                cutoff = confirmed + timedelta(hours=168)
                if cutoff <= now_utc:
                    value = row.get("return_168h_pct")
                    if value is None:
                        value = row.get("path_return_168h")
                    if value is not None:
                        return cutoff, float(value)
                return None, None
            if strategy == "tp20":
                target = earliest_event(row, "target_20_path_at", "target_20_at")
                if target is not None and target <= now_utc:
                    return target, 0.20
                return None, None
            if strategy == "standard_7d":
                exit_at = confirmed + timedelta(hours=168)
                if exit_at <= now_utc and row.get("return_168h_pct") is not None:
                    return exit_at, float(row["return_168h_pct"])
                return None, None
            raise ValueError(f"unsupported account replay strategy: {strategy}")

        def close_due(cutoff: datetime) -> None:
            nonlocal equity, closed
            due = sorted(
                [pos for pos in positions if pos["exit_at"] is not None and pos["exit_at"] <= cutoff],
                key=lambda pos: pos["exit_at"],
            )
            for pos in due:
                equity += pos["notional"] * (float(pos["exit_return"]) - SHADOW_FEE_PER_FILL)
                holds_hours.append(max(0.0, (pos["exit_at"] - pos["entry_at"]).total_seconds() / 3600.0))
                positions.remove(pos)
                closed += 1

        for row in ordered:
            entry_at = row["confirmed_at"]
            close_due(entry_at)
            tier = str(row.get("risk_tier") or "standard")
            if tier not in risk_tiers:
                continue
            eligible_signals += 1
            symbol = str(row.get("symbol") or "")
            if any(pos["symbol"] == symbol for pos in positions):
                missed_same_symbol += 1
                continue
            if len(positions) >= exposure.max_slots:
                missed_capacity += 1
                continue

            exit_at, exit_return = known_exit(row)
            notional = max(0.0, equity) * exposure.per_trade_pct
            equity -= notional * SHADOW_FEE_PER_FILL
            path_times = list(row.get("path_times") or ())
            path_returns = list(row.get("path_returns") or ())
            path_points = [
                (ts, float(ret))
                for ts, ret in zip(path_times, path_returns)
                if ts is not None and ret is not None and entry_at <= ts <= now_utc
            ]
            position = {
                "position_id": entered,
                "symbol": symbol,
                "entry_at": entry_at,
                "notional": notional,
                "exit_at": exit_at,
                "exit_return": exit_return,
                "mark_return": row.get("current_return_pct"),
                "path_points": path_points,
            }
            positions.append(position)
            all_positions.append(position)
            entered += 1
            max_open = max(max_open, len(positions))

        close_due(now_utc)

        marked_equity = equity
        unmarked = 0
        for pos in positions:
            holds_hours.append(max(0.0, (now_utc - pos["entry_at"]).total_seconds() / 3600.0))
            mark = pos["mark_return"]
            if mark is None:
                unmarked += 1
                continue
            # Include a hypothetical closing fee so the MTM account return is
            # comparable with already-realized positions.
            marked_equity += pos["notional"] * (float(mark) - SHADOW_FEE_PER_FILL)

        # Reconstruct the marked account-equity path from the exact positions
        # admitted by the chronological capacity replay. Research 15m closes are
        # used when present, while entry/exit/report marks guarantee a defined
        # sparse path for tests and older rows. Drawdown follows account MTM
        # equity (paid fees + unrealized P&L); the report-time return separately
        # includes a hypothetical closing fee for still-open positions.
        event_times: set[datetime] = set()
        if account_start_at is not None:
            event_times.add(account_start_at)
        event_times.add(now_utc)
        for pos in all_positions:
            event_times.add(pos["entry_at"])
            if pos["exit_at"] is not None:
                event_times.add(pos["exit_at"])
            for ts, _ in pos["path_points"]:
                cutoff = pos["exit_at"] or now_utc
                if pos["entry_at"] <= ts <= cutoff:
                    event_times.add(ts)

        cash = 1.0
        active_marks: dict[int, tuple[float, float]] = {}
        peak_equity = 1.0
        max_mtm_drawdown = 0.0
        positions_by_entry: dict[datetime, list[dict[str, Any]]] = {}
        positions_by_exit: dict[datetime, list[dict[str, Any]]] = {}
        path_updates: dict[datetime, list[tuple[int, float, float]]] = {}
        for pos in all_positions:
            positions_by_entry.setdefault(pos["entry_at"], []).append(pos)
            if pos["exit_at"] is not None:
                positions_by_exit.setdefault(pos["exit_at"], []).append(pos)
            cutoff = pos["exit_at"] or now_utc
            for ts, ret in pos["path_points"]:
                if pos["entry_at"] <= ts < cutoff:
                    path_updates.setdefault(ts, []).append((pos["position_id"], pos["notional"], ret))
            if pos["exit_at"] is None and pos["mark_return"] is not None:
                path_updates.setdefault(now_utc, []).append(
                    (pos["position_id"], pos["notional"], float(pos["mark_return"]))
                )

        for ts in sorted(event_times):
            # The admission replay releases due positions before considering new
            # entries at the same timestamp, so mirror that ordering here.
            for pos in positions_by_exit.get(ts, ()):
                active_marks.pop(pos["position_id"], None)
                cash += pos["notional"] * (float(pos["exit_return"]) - SHADOW_FEE_PER_FILL)
            for pos in positions_by_entry.get(ts, ()):
                cash -= pos["notional"] * SHADOW_FEE_PER_FILL
                active_marks[pos["position_id"]] = (pos["notional"], 0.0)
            for position_id, notional, mark in path_updates.get(ts, ()):
                if position_id in active_marks:
                    active_marks[position_id] = (notional, mark)

            # Account MTM drawdown uses paid fees plus live unrealized P&L; a
            # future closing fee is not an account-equity loss until the exit.
            path_equity = cash + sum(
                notional * mark
                for notional, mark in active_marks.values()
            )
            if peak_equity > 0:
                max_mtm_drawdown = max(
                    max_mtm_drawdown,
                    max(0.0, (peak_equity - path_equity) / peak_equity),
                )
            peak_equity = max(peak_equity, path_equity)

        slot_days = sum(holds_hours) / 24.0
        avg_exposure = (
            slot_days * exposure.per_trade_pct / span_days
            if span_days > 0 else None
        )
        observed_return = None if unmarked else (marked_equity - 1.0)
        run_rate = (
            observed_return * MONTHLY_RUN_RATE_DAYS / span_days
            if observed_return is not None and span_days > 0 else None
        )
        return_over_drawdown = (
            observed_return / max_mtm_drawdown
            if observed_return is not None and max_mtm_drawdown > 0 else None
        )
        return AccountRunRateSummary(
            strategy=strategy,
            start_at=account_start_at,
            end_at=now_utc,
            span_days=span_days,
            eligible_signals=eligible_signals,
            entered=entered,
            closed=closed,
            open_positions=len(positions),
            unmarked_open_positions=unmarked,
            missed_capacity=missed_capacity,
            missed_same_symbol=missed_same_symbol,
            observed_account_return=observed_return,
            thirty_day_equivalent_return=run_rate,
            thirty_day_pnl_per_10k=(run_rate * 10000.0) if run_rate is not None else None,
            avg_exposure_pct=avg_exposure,
            peak_exposure_pct=max_open * exposure.per_trade_pct,
            max_mtm_drawdown=max_mtm_drawdown if event_times else None,
            return_over_max_drawdown=return_over_drawdown,
            slot_days=slot_days,
        )

    trader_strategy_tp5 = trader_strategy_summary("tp5")
    trader_strategy_tp5_sl75 = trader_strategy_summary("tp5_sl75")
    trader_strategy_hold_7d = trader_strategy_summary("hold_7d")

    tp5_exposure = ExposureRecommendation(0.05, 6, 0.30, "portfolio-tested frozen TP5 configuration")
    tp20_exposure = ExposureRecommendation(0.02, 5, 0.10, "risk-based suggestion from observed HIGH_RISK adverse paths")
    standard_7d_exposure = ExposureRecommendation(0.03, 5, 0.15, "risk-based suggestion for fixed 7-day STANDARD holds")
    tp5_account_run_rate = account_run_rate(
        strategy="tp5", risk_tiers={"standard", "high_risk"}, exposure=tp5_exposure
    )
    tp5_sl75_account_run_rate = account_run_rate(
        strategy="tp5_sl75", risk_tiers={"standard", "high_risk"}, exposure=tp5_exposure
    )
    hold_7d_account_run_rate = account_run_rate(
        strategy="hold_7d", risk_tiers={"standard", "high_risk"}, exposure=tp5_exposure
    )
    tp20_account_run_rate = account_run_rate(
        strategy="tp20", risk_tiers={"high_risk"}, exposure=tp20_exposure
    )
    standard_7d_account_run_rate = account_run_rate(
        strategy="standard_7d", risk_tiers={"standard"}, exposure=standard_7d_exposure
    )

    return PerformanceSummary(
        report_date=report_date,
        confirmed_today=confirmed_today,
        open_count=len(open_rows),
        open_avg_return=average(open_returns),
        open_sum_return=sum(open_returns) if open_returns else None,
        horizon_24h=h24,
        horizon_48h=h48,
        horizon_72h=h72,
        horizon_168h=h168,
        standard_survival=tuple(horizon_survival(h, standard=True) for h in (24, 48, 72, 168)),
        risky_survival=tuple(horizon_survival(h, standard=False) for h in (24, 48, 72, 168)),
        standard_weekly=weekly_risk(standard=True),
        risky_weekly=weekly_risk(standard=False),
        high_weekly=weekly_risk_for(risk_label="HIGH RISK", risk_tiers={"high_risk"}),
        extreme_weekly=weekly_risk_for(risk_label="EXTREME RISK", risk_tiers=set()),
        standard_profit_target=profit_target_summary(standard=True),
        risky_profit_target=profit_target_summary(standard=False),
        standard_strategy_matrix=strategy_matrix(standard=True),
        risky_strategy_matrix=strategy_matrix(standard=False),
        high_strategy_matrix=strategy_matrix_for(risk_label="HIGH RISK", risk_tiers={"high_risk"}),
        extreme_strategy_matrix=strategy_matrix_for(risk_label="EXTREME RISK", risk_tiers=set()),
        avg_return_1h=average(values_for("return_1h_pct")),
        avg_return_4h=average(values_for("return_4h_pct")),
        avg_return_12h=average(values_for("return_12h_pct")),
        avg_mfe_7d=average(mfe),
        avg_mae_7d=average(mae),
        best_symbol_7d=str(best["symbol"]) if best is not None else None,
        best_return_7d=float(best["return_168h_pct"]) if best is not None else None,
        worst_symbol_7d=str(worst["symbol"]) if worst is not None else None,
        worst_return_7d=float(worst["return_168h_pct"]) if worst is not None else None,
        tp5_public=tp5_public,
        high_risk_tp20_public=high_risk_tp20_public,
        standard_7d_public=standard_7d_public,
        comparison_start_at=comparison_start_at,
        comparison_end_at=comparison_end_at,
        tp5_7d_comparison=tp5_7d_comparison,
        tp20_7d_comparison=tp20_7d_comparison,
        standard_7d_comparison=standard_7d_comparison,
        trader_strategy_tp5=trader_strategy_tp5,
        trader_strategy_tp5_sl75=trader_strategy_tp5_sl75,
        trader_strategy_hold_7d=trader_strategy_hold_7d,
        tp5_exposure=tp5_exposure,
        tp20_exposure=tp20_exposure,
        standard_7d_exposure=standard_7d_exposure,
        tp5_account_run_rate=tp5_account_run_rate,
        tp5_sl75_account_run_rate=tp5_sl75_account_run_rate,
        hold_7d_account_run_rate=hold_7d_account_run_rate,
        tp20_account_run_rate=tp20_account_run_rate,
        standard_7d_account_run_rate=standard_7d_account_run_rate,
    )
