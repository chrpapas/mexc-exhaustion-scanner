from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PUBLIC_PERFORMANCE_RISK_TIERS = frozenset({"standard", "high_risk"})

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

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class Standard7dPublicSummary:
    matured_7d: int
    positive_rate: float | None
    avg_return: float | None
    median_return: float | None

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
    standard_7d_public: Standard7dPublicSummary = Standard7dPublicSummary(0, None, None, None)

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
            "standard_7d_public": self.standard_7d_public.as_dict(),
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
    )
    standard_7d_values = [
        float(row["return_168h_pct"])
        for row in matured_7d
        if str(row.get("risk_tier") or "standard") == "standard"
    ]
    standard_7d_public = Standard7dPublicSummary(
        matured_7d=len(standard_7d_values),
        positive_rate=rate([value > 0 for value in standard_7d_values]),
        avg_return=average(standard_7d_values),
        median_return=percentile(standard_7d_values, 0.50),
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
        standard_7d_public=standard_7d_public,
    )
