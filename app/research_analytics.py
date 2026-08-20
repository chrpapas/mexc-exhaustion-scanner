from __future__ import annotations

import csv
import io
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from app.json_utils import json_object

PUBLIC_RESEARCH_RISK_TIERS = frozenset({"standard", "high_risk"})
TARGET_LEVELS_PCT: tuple[int, ...] = (5, 10, 15, 20, 25, 30, 40)
STANDARD_EXIT_HORIZONS_HOURS: tuple[int, ...] = (24, 48, 72, 96, 120, 144, 168, 192, 240, 288, 336)
HIGH_RISK_TIMEOUT_HOURS: tuple[int, ...] = (24, 48, 72, 96, 120, 168, 240, 336)
STOP_THRESHOLDS_PCT: tuple[int, ...] = (10, 20, 30, 50, 75, 100)
DELAYED_ENTRY_MINUTES: tuple[int, ...] = (0, 15, 30, 60, 120, 240, 480)


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    key: str
    label: str
    source: str = "snapshot"
    kind: str = "numeric"


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("return_24h", "24h pump return"),
    FeatureSpec("return_72h", "72h pump return"),
    FeatureSpec("residual_return_24h", "24h BTC residual"),
    FeatureSpec("cross_section_percentile", "Cross-section percentile"),
    FeatureSpec("volume_zscore_15m", "15m volume z-score"),
    FeatureSpec("distance_above_ema20_atr_4h", "4h EMA20 distance (ATR)"),
    FeatureSpec("amount_24h", "24h turnover"),
    FeatureSpec("spread_pct", "Bid/ask spread"),
    FeatureSpec("funding_rate", "Funding rate"),
    FeatureSpec("fair_index_premium_pct", "Fair/index premium"),
    FeatureSpec("hold_vol", "Open interest / hold volume"),
    FeatureSpec("upper_wick_ratio_15m", "15m upper wick ratio"),
    FeatureSpec("close_location_15m", "15m close location"),
    FeatureSpec("momentum_1h", "1h momentum"),
    FeatureSpec("previous_momentum_1h", "Previous 1h momentum"),
    FeatureSpec("run_score", "Run score", source="row"),
    FeatureSpec("exhaustion_score", "Exhaustion score", source="row"),
    FeatureSpec("hours_run_to_breakdown", "Run → breakdown hours", source="row"),
    FeatureSpec("hours_breakdown_to_retest", "Breakdown → retest hours", source="row"),
    FeatureSpec("hours_breakdown_to_confirmation", "Breakdown → confirmation hours", source="row"),
    FeatureSpec("hours_episode_to_confirmation", "Episode → confirmation hours", source="row"),
    FeatureSpec("momentum_decelerating", "Momentum decelerating", kind="bool"),
    FeatureSpec("below_ema9_15m", "Below EMA9 (15m)", kind="bool"),
    FeatureSpec("lower_high_and_close", "Lower high + lower close", kind="bool"),
    FeatureSpec("structural_break_15m", "Structural break (15m)", kind="bool"),
)
FEATURE_BY_KEY = {spec.key: spec for spec in FEATURE_SPECS}
INTERACTION_PAIRS: tuple[tuple[str, str], ...] = (
    ("exhaustion_score", "volume_zscore_15m"),
    ("exhaustion_score", "funding_rate"),
    ("run_score", "volume_zscore_15m"),
    ("amount_24h", "volume_zscore_15m"),
    ("fair_index_premium_pct", "funding_rate"),
    ("return_24h", "momentum_1h"),
    ("return_72h", "run_score"),
)


@dataclass(frozen=True, slots=True)
class BaselineSummary:
    total_signals: int
    matured_7d: int
    complete_paths_7d: int
    complete_paths_14d: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None
    median_return_7d: float | None
    median_mfe_7d: float | None
    median_adverse_7d: float | None
    median_adverse_before_20: float | None
    median_time_to_20_hours: float | None
    median_time_to_mfe_hours: float | None
    median_time_to_mae_hours: float | None


@dataclass(frozen=True, slots=True)
class TargetSweepSummary:
    target_pct: int
    sample: int
    hits: int
    hit_rate: float | None
    median_time_hours: float | None
    p75_time_hours: float | None


@dataclass(frozen=True, slots=True)
class FeatureSliceSummary:
    feature: str
    feature_label: str
    bucket: str
    sample: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None
    median_return_7d: float | None
    median_mfe_7d: float | None
    median_adverse_7d: float | None
    target_lift_pp: float | None
    positive_lift_pp: float | None
    avg_return_lift_pp: float | None
    rank_score: float | None


@dataclass(frozen=True, slots=True)
class ExitHorizonSummary:
    risk_tier: str
    horizon_hours: int
    cohort_horizon_hours: int
    sample: int
    positive_rate: float | None
    avg_return: float | None
    median_return: float | None
    worst_return: float | None
    best_return: float | None
    avg_return_per_day: float | None


@dataclass(frozen=True, slots=True)
class HighRiskTimeoutSummary:
    timeout_hours: int
    sample: int
    target_hits: int
    target_hit_rate: float | None
    avg_strategy_return: float | None
    median_strategy_return: float | None
    positive_rate: float | None
    worst_strategy_return: float | None
    avg_holding_hours: float | None
    return_per_slot_day: float | None


@dataclass(frozen=True, slots=True)
class StopSurvivalSummary:
    risk_tier: str
    stop_pct: int
    winners_with_path: int
    winners_killed: int
    kill_rate: float | None
    survivor_rate: float | None


@dataclass(frozen=True, slots=True)
class ScoreBucketSummary:
    score_name: str
    bucket: str
    sample: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None
    median_return_7d: float | None


@dataclass(frozen=True, slots=True)
class InteractionSummary:
    interaction: str
    bucket: str
    sample: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None
    target_lift_pp: float | None
    positive_lift_pp: float | None
    avg_return_lift_pp: float | None
    rank_score: float | None


@dataclass(frozen=True, slots=True)
class DelayedEntrySummary:
    delay_minutes: int
    sample: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None
    median_return_7d: float | None
    median_mfe_7d: float | None
    median_adverse_7d: float | None
    median_time_to_20_hours: float | None


@dataclass(frozen=True, slots=True)
class ResearchAnalyticsReport:
    generated_at: datetime
    baseline: BaselineSummary
    target_sweep: tuple[TargetSweepSummary, ...]
    feature_slices: tuple[FeatureSliceSummary, ...]
    standard_exit_sweep: tuple[ExitHorizonSummary, ...]
    high_risk_timeout_sweep: tuple[HighRiskTimeoutSummary, ...]
    stop_survival: tuple[StopSurvivalSummary, ...]
    score_buckets: tuple[ScoreBucketSummary, ...]
    interactions: tuple[InteractionSummary, ...]
    delayed_entries: tuple[DelayedEntrySummary, ...]
    min_rank_sample: int

    @property
    def ranked_slices(self) -> tuple[FeatureSliceSummary, ...]:
        eligible = [
            item
            for item in self.feature_slices
            if item.sample >= self.min_rank_sample and item.rank_score is not None
        ]
        return tuple(sorted(eligible, key=lambda item: float(item.rank_score), reverse=True))

    @property
    def best_slices(self) -> tuple[FeatureSliceSummary, ...]:
        return self.ranked_slices[:5]

    @property
    def worst_slices(self) -> tuple[FeatureSliceSummary, ...]:
        return tuple(reversed(self.ranked_slices[-5:]))

    @property
    def ranked_interactions(self) -> tuple[InteractionSummary, ...]:
        eligible = [
            item
            for item in self.interactions
            if item.sample >= max(3, self.min_rank_sample // 2) and item.rank_score is not None
        ]
        return tuple(sorted(eligible, key=lambda item: float(item.rank_score), reverse=True))



def _rate(values: list[bool]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _feature_value(row: dict[str, Any], spec: FeatureSpec) -> Any:
    if spec.source == "row":
        return row.get(spec.key)
    snapshot = row.get("_snapshot")
    if snapshot is None:
        snapshot = json_object(row.get("feature_snapshot"))
        row["_snapshot"] = snapshot
    return snapshot.get(spec.key)


def _target_within_hours(row: dict[str, Any], hours: int, *, prefer_path: bool = False) -> bool:
    confirmed = row.get("confirmed_at")
    if confirmed is None:
        return False
    if prefer_path:
        candidates = [value for value in (row.get("target_20_path_at"), row.get("target_20_at")) if value is not None]
        target = min(candidates) if candidates else None
    else:
        target = row.get("target_20_at")
    return bool(target is not None and target <= confirmed + timedelta(hours=hours))


def _path_complete_for_hours(row: dict[str, Any], hours: int) -> bool:
    confirmed = row.get("confirmed_at")
    last = row.get("path_last_at")
    if confirmed is None or last is None:
        return False
    if last < confirmed + timedelta(hours=hours) - timedelta(minutes=15):
        return False
    count_key = {168: "path_rows_7d", 336: "path_rows_14d"}.get(hours, f"path_rows_{hours}h")
    observed = _float(row.get(count_key))
    if observed is not None:
        expected = hours * 4  # 15-minute bars
        if observed < math.ceil(expected * 0.98):
            return False
    return True


def _path_complete_7d(row: dict[str, Any]) -> bool:
    return _path_complete_for_hours(row, 168)


def _elapsed_hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _time_to_target_hours(row: dict[str, Any], target_pct: int, *, max_hours: int = 168) -> float | None:
    key = "target_20_path_at" if target_pct == 20 else f"target_{target_pct}_at"
    hit = row.get(key)
    confirmed = row.get("confirmed_at")
    if hit is None or confirmed is None:
        return None
    elapsed = max(0.0, (hit - confirmed).total_seconds() / 3600.0)
    return elapsed if elapsed <= max_hours else None


def _horizon_return(row: dict[str, Any], hours: int) -> float | None:
    # Preserve the existing broad shadow-trade sample at horizons it already tracks.
    shadow_key = {24: "return_24h_pct", 48: "return_48h_pct", 72: "return_72h_pct", 168: "return_168h_pct"}.get(hours)
    if shadow_key:
        shadow_value = _float(row.get(shadow_key))
        if shadow_value is not None:
            return shadow_value
    if not _path_complete_for_hours(row, hours):
        return None
    return _float(row.get(f"path_return_{hours}h"))


def shadow_entry_scores(row: dict[str, Any]) -> tuple[int, int]:
    """Candidate evidence score; intentionally shadow-only and fixed for reproducibility.

    Thresholds are frozen from the first v1.2.7 feature-lift sample (20 Aug 2026).
    They are hypotheses to evaluate, not live trading rules.
    """
    snap = json_object(row.get("feature_snapshot"))
    exhaustion = _float(row.get("exhaustion_score"))
    run_score = _float(row.get("run_score"))
    amount = _float(snap.get("amount_24h"))
    volume_z = _float(snap.get("volume_zscore_15m"))
    premium = _float(snap.get("fair_index_premium_pct"))
    funding = _float(snap.get("funding_rate"))
    return_24h = _float(snap.get("return_24h"))
    return_72h = _float(snap.get("return_72h"))
    momentum = _float(snap.get("momentum_1h"))
    ema_distance = _float(snap.get("distance_above_ema20_atr_4h"))
    run_hours = _float(row.get("hours_run_to_breakdown"))
    episode_hours = _float(row.get("hours_episode_to_confirmation"))

    quality = 0
    quality += 2 if exhaustion is not None and 4 < exhaustion <= 5 else 0
    quality += 2 if run_score is not None and 3.667 < run_score <= 5 else 0
    quality += 1 if amount is not None and amount > 12_310_000 else 0
    quality += 1 if volume_z is not None and volume_z <= -0.2796 else 0
    quality += 1 if premium is not None and premium <= -0.04249 else 0
    quality += 1 if return_24h is not None and return_24h > 0.2482 else 0
    quality += 1 if return_72h is not None and return_72h > 0.5629 else 0
    quality += 1 if momentum is not None and momentum <= -0.04472 else 0

    continuation = 0
    continuation += 2 if exhaustion is not None and exhaustion > 5 else 0
    continuation += 2 if run_score is not None and run_score > 5 else 0
    continuation += 1 if volume_z is not None and volume_z > 1.027 else 0
    continuation += 1 if funding is not None and funding > 0.000243 else 0
    continuation += 1 if ema_distance is not None and ema_distance > 3.044 else 0
    continuation += 1 if momentum is not None and momentum > -0.04472 else 0
    continuation += 1 if run_hours is not None and run_hours > 20.5 else 0
    continuation += 1 if episode_hours is not None and episode_hours > 21 else 0
    return min(10, quality), min(10, continuation)


def _slice_summary(
    rows: list[dict[str, Any]],
    *,
    spec: FeatureSpec,
    bucket: str,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> FeatureSliceSummary:
    target_rate = _rate([_target_within_hours(row, 168) for row in rows])
    return_values = [value for row in rows for value in [_float(row.get("return_168h_pct"))] if value is not None]
    positive_rate = _rate([value > 0 for value in return_values])
    avg_return = _mean(return_values)
    mfe = [_float(row.get("path_mfe_7d")) for row in rows if _path_complete_7d(row)]
    adverse = [
        -value
        for row in rows
        if _path_complete_7d(row)
        for value in [_float(row.get("path_mae_7d"))]
        if value is not None
    ]
    target_lift = target_rate - baseline_target if target_rate is not None and baseline_target is not None else None
    positive_lift = positive_rate - baseline_positive if positive_rate is not None and baseline_positive is not None else None
    avg_lift = avg_return - baseline_avg_return if avg_return is not None and baseline_avg_return is not None else None
    components = [value for value in (target_lift, positive_lift, avg_lift) if value is not None]
    return FeatureSliceSummary(
        feature=spec.key,
        feature_label=spec.label,
        bucket=bucket,
        sample=len(rows),
        target_20_rate_7d=target_rate,
        positive_7d_rate=positive_rate,
        avg_return_7d=avg_return,
        median_return_7d=_median(return_values),
        median_mfe_7d=_median([v for v in mfe if v is not None]),
        median_adverse_7d=_median(adverse),
        target_lift_pp=target_lift,
        positive_lift_pp=positive_lift,
        avg_return_lift_pp=avg_lift,
        rank_score=_mean(components),
    )


def _numeric_bucket_assignments(rows: list[dict[str, Any]], spec: FeatureSpec) -> dict[int, str]:
    valued: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        value = _float(_feature_value(row, spec))
        if value is not None:
            valued.append((row, value))
    if len(valued) < 3:
        return {}
    values = [value for _, value in valued]
    q33 = _percentile(values, 1 / 3)
    q67 = _percentile(values, 2 / 3)
    if q33 is None or q67 is None or q33 == q67:
        return {}
    assignments: dict[int, str] = {}
    for row, value in valued:
        if value <= q33:
            label = f"LOW≤{q33:.4g}"
        elif value <= q67:
            label = f"MID {q33:.4g}–{q67:.4g}"
        else:
            label = f"HIGH>{q67:.4g}"
        assignments[id(row)] = label
    return assignments


def _numeric_feature_slices(
    rows: list[dict[str, Any]],
    spec: FeatureSpec,
    *,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> list[FeatureSliceSummary]:
    assignments = _numeric_bucket_assignments(rows, spec)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        label = assignments.get(id(row))
        if label:
            groups.setdefault(label, []).append(row)
    return [
        _slice_summary(
            group,
            spec=spec,
            bucket=label,
            baseline_target=baseline_target,
            baseline_positive=baseline_positive,
            baseline_avg_return=baseline_avg_return,
        )
        for label, group in groups.items()
    ]


def _bool_feature_slices(
    rows: list[dict[str, Any]],
    spec: FeatureSpec,
    *,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> list[FeatureSliceSummary]:
    valued = [(row, _bool(_feature_value(row, spec))) for row in rows]
    result: list[FeatureSliceSummary] = []
    for value, label in ((True, "TRUE"), (False, "FALSE")):
        group = [row for row, parsed in valued if parsed is value]
        if group:
            result.append(
                _slice_summary(
                    group,
                    spec=spec,
                    bucket=label,
                    baseline_target=baseline_target,
                    baseline_positive=baseline_positive,
                    baseline_avg_return=baseline_avg_return,
                )
            )
    return result


def _build_standard_exit_sweep(rows: list[dict[str, Any]]) -> tuple[ExitHorizonSummary, ...]:
    """Compare fixed-time Standard exits on paired cohorts.

    Horizons through 7d use only Standard signals with a complete 7d path, so every
    reported 1d..7d value is calculated from the exact same episodes. Extended
    horizons use only complete 14d paths, again preserving a common cohort. This
    prevents younger signals from making short horizons look artificially different.
    """
    standard = [row for row in rows if str(row.get("risk_tier") or "standard") == "standard"]
    cohort_7d = [row for row in standard if _path_complete_for_hours(row, 168)]
    cohort_14d = [row for row in standard if _path_complete_for_hours(row, 336)]
    result: list[ExitHorizonSummary] = []
    for hours in STANDARD_EXIT_HORIZONS_HOURS:
        cohort_horizon = 168 if hours <= 168 else 336
        cohort = cohort_7d if cohort_horizon == 168 else cohort_14d
        values = [
            value
            for row in cohort
            for value in [_horizon_return(row, hours)]
            if value is not None
        ]
        # A paired cohort should have a value for every episode. If a row is missing
        # the requested horizon despite being path-complete, exclude the entire
        # horizon from comparison rather than silently changing the denominator.
        if len(values) != len(cohort):
            values = []
        days = hours / 24.0
        result.append(
            ExitHorizonSummary(
                risk_tier="standard",
                horizon_hours=hours,
                cohort_horizon_hours=cohort_horizon,
                sample=len(values),
                positive_rate=_rate([value > 0 for value in values]),
                avg_return=_mean(values),
                median_return=_median(values),
                worst_return=min(values) if values else None,
                best_return=max(values) if values else None,
                avg_return_per_day=(_mean(values) / days) if values and days else None,
            )
        )
    return tuple(result)


def _eligible_at_timeout(row: dict[str, Any], hours: int, generated_at: datetime) -> bool:
    confirmed = row.get("confirmed_at")
    return bool(confirmed is not None and confirmed + timedelta(hours=hours) <= generated_at)


def _build_high_risk_timeout_sweep(
    rows: list[dict[str, Any]], *, generated_at: datetime
) -> tuple[HighRiskTimeoutSummary, ...]:
    """Simulate TP20-or-timeout without right-censoring.

    A signal is eligible for an Nh timeout only after it has actually aged N hours.
    Hitting TP20 early does not make a young signal eligible for a future timeout.
    This removes the survivorship bias that previously produced artificial 100% TP
    rates at 10d/14d before those cohorts had matured.
    """
    risky = [row for row in rows if str(row.get("risk_tier") or "standard") == "high_risk"]
    result: list[HighRiskTimeoutSummary] = []
    for hours in HIGH_RISK_TIMEOUT_HOURS:
        outcomes: list[float] = []
        holding_hours: list[float] = []
        target_hits = 0
        for row in risky:
            if not _eligible_at_timeout(row, hours, generated_at):
                continue
            if _target_within_hours(row, hours, prefer_path=True):
                target_at_candidates = [
                    value for value in (row.get("target_20_path_at"), row.get("target_20_at"))
                    if value is not None
                ]
                target_at = min(target_at_candidates) if target_at_candidates else None
                elapsed = _elapsed_hours(row.get("confirmed_at"), target_at)
                if elapsed is None:
                    continue
                outcomes.append(0.20)
                holding_hours.append(min(float(hours), elapsed))
                target_hits += 1
                continue
            timeout_return = _horizon_return(row, hours)
            if timeout_return is not None:
                outcomes.append(timeout_return)
                holding_hours.append(float(hours))
        total_slot_days = sum(holding_hours) / 24.0
        result.append(
            HighRiskTimeoutSummary(
                timeout_hours=hours,
                sample=len(outcomes),
                target_hits=target_hits,
                target_hit_rate=(target_hits / len(outcomes)) if outcomes else None,
                avg_strategy_return=_mean(outcomes),
                median_strategy_return=_median(outcomes),
                positive_rate=_rate([value > 0 for value in outcomes]),
                worst_strategy_return=min(outcomes) if outcomes else None,
                avg_holding_hours=_mean(holding_hours),
                return_per_slot_day=(sum(outcomes) / total_slot_days) if total_slot_days > 0 else None,
            )
        )
    return tuple(result)


def _build_stop_survival(rows: list[dict[str, Any]]) -> tuple[StopSurvivalSummary, ...]:
    result: list[StopSurvivalSummary] = []
    for tier in ("all", "standard", "high_risk"):
        candidate_rows = rows if tier == "all" else [row for row in rows if str(row.get("risk_tier") or "standard") == tier]
        winners = [
            row
            for row in candidate_rows
            if _time_to_target_hours(row, 20, max_hours=168) is not None
            and _float(row.get("path_mae_before_target_20")) is not None
        ]
        adverse = [-float(row["path_mae_before_target_20"]) for row in winners]
        for stop_pct in STOP_THRESHOLDS_PCT:
            threshold = stop_pct / 100.0
            killed = sum(1 for value in adverse if value >= threshold)
            result.append(
                StopSurvivalSummary(
                    risk_tier=tier,
                    stop_pct=stop_pct,
                    winners_with_path=len(adverse),
                    winners_killed=killed,
                    kill_rate=(killed / len(adverse)) if adverse else None,
                    survivor_rate=(1.0 - killed / len(adverse)) if adverse else None,
                )
            )
    return tuple(result)


def _score_bucket(score: int) -> str:
    if score <= 3:
        return "LOW 0–3"
    if score <= 6:
        return "MID 4–6"
    return "HIGH 7–10"


def _build_score_buckets(matured: list[dict[str, Any]]) -> tuple[ScoreBucketSummary, ...]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in matured:
        quality, continuation = shadow_entry_scores(row)
        row["_entry_quality_score"] = quality
        row["_continuation_risk_score"] = continuation
        groups.setdefault(("entry_quality", _score_bucket(quality)), []).append(row)
        groups.setdefault(("continuation_risk", _score_bucket(continuation)), []).append(row)
    result: list[ScoreBucketSummary] = []
    for (name, bucket), group in sorted(groups.items()):
        returns = [value for row in group for value in [_float(row.get("return_168h_pct"))] if value is not None]
        result.append(
            ScoreBucketSummary(
                score_name=name,
                bucket=bucket,
                sample=len(group),
                target_20_rate_7d=_rate([_target_within_hours(row, 168) for row in group]),
                positive_7d_rate=_rate([value > 0 for value in returns]),
                avg_return_7d=_mean(returns),
                median_return_7d=_median(returns),
            )
        )
    return tuple(result)


def _build_interactions(
    matured: list[dict[str, Any]],
    *,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> tuple[InteractionSummary, ...]:
    result: list[InteractionSummary] = []
    for left_key, right_key in INTERACTION_PAIRS:
        left_spec = FEATURE_BY_KEY[left_key]
        right_spec = FEATURE_BY_KEY[right_key]
        left = _numeric_bucket_assignments(matured, left_spec)
        right = _numeric_bucket_assignments(matured, right_spec)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in matured:
            l = left.get(id(row))
            r = right.get(id(row))
            if l and r:
                groups.setdefault(f"{l} × {r}", []).append(row)
        for bucket, group in groups.items():
            returns = [value for row in group for value in [_float(row.get("return_168h_pct"))] if value is not None]
            target_rate = _rate([_target_within_hours(row, 168) for row in group])
            positive = _rate([value > 0 for value in returns])
            avg_return = _mean(returns)
            target_lift = target_rate - baseline_target if target_rate is not None and baseline_target is not None else None
            positive_lift = positive - baseline_positive if positive is not None and baseline_positive is not None else None
            avg_lift = avg_return - baseline_avg_return if avg_return is not None and baseline_avg_return is not None else None
            components = [value for value in (target_lift, positive_lift, avg_lift) if value is not None]
            result.append(
                InteractionSummary(
                    interaction=f"{left_spec.label} × {right_spec.label}",
                    bucket=bucket,
                    sample=len(group),
                    target_20_rate_7d=target_rate,
                    positive_7d_rate=positive,
                    avg_return_7d=avg_return,
                    target_lift_pp=target_lift,
                    positive_lift_pp=positive_lift,
                    avg_return_lift_pp=avg_lift,
                    rank_score=_mean(components),
                )
            )
    return tuple(result)


def _build_delayed_entries(raw_rows: Iterable[dict[str, Any]]) -> tuple[DelayedEntrySummary, ...]:
    """Compare delays on one common complete cohort.

    Only episodes that have a complete 7d delayed path for every configured delay
    are used. This makes +0m, +15m, +30m, ... directly comparable instead of letting
    each delay silently use a different set of signals.
    """
    rows = [dict(row) for row in raw_rows]
    rows = [row for row in rows if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    by_episode: dict[Any, dict[int, dict[str, Any]]] = {}
    for row in rows:
        episode_id = row.get("episode_id")
        if episode_id is None:
            continue
        delay = int(row.get("delay_minutes") or 0)
        by_episode.setdefault(episode_id, {})[delay] = row

    complete_episode_ids = {
        episode_id
        for episode_id, variants in by_episode.items()
        if all(
            delay in variants and bool(variants[delay].get("path_complete_7d"))
            for delay in DELAYED_ENTRY_MINUTES
        )
    }

    result: list[DelayedEntrySummary] = []
    for delay in DELAYED_ENTRY_MINUTES:
        group = [by_episode[episode_id][delay] for episode_id in complete_episode_ids]
        returns = [value for row in group for value in [_float(row.get("return_7d_pct"))] if value is not None]
        targets = [row for row in group if row.get("target_20_at") is not None]
        target_times = [
            value
            for row in targets
            for value in [_elapsed_hours(row.get("entry_at"), row.get("target_20_at"))]
            if value is not None
        ]
        mfes = [value for row in group for value in [_float(row.get("mfe_7d"))] if value is not None]
        adverse = [-value for row in group for value in [_float(row.get("mae_7d"))] if value is not None]
        result.append(
            DelayedEntrySummary(
                delay_minutes=delay,
                sample=len(group),
                target_20_rate_7d=(len(targets) / len(group)) if group else None,
                positive_7d_rate=_rate([value > 0 for value in returns]),
                avg_return_7d=_mean(returns),
                median_return_7d=_median(returns),
                median_mfe_7d=_median(mfes),
                median_adverse_7d=_median(adverse),
                median_time_to_20_hours=_median(target_times),
            )
        )
    return tuple(result)


def build_research_analytics(
    raw_rows: Iterable[dict[str, Any]],
    *,
    generated_at: datetime,
    delayed_entry_rows: Iterable[dict[str, Any]] = (),
) -> ResearchAnalyticsReport:
    rows = [dict(row) for row in raw_rows]
    rows = [row for row in rows if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    matured = [row for row in rows if row.get("return_168h_pct") is not None]
    complete_paths_7d = [row for row in matured if _path_complete_7d(row)]
    complete_paths_14d = [row for row in rows if _path_complete_for_hours(row, 336)]

    return_values = [value for row in matured for value in [_float(row.get("return_168h_pct"))] if value is not None]
    target_flags = [_target_within_hours(row, 168) for row in matured]
    positive_flags = [value > 0 for value in return_values]
    target_times = [
        value
        for row in matured
        if _target_within_hours(row, 168)
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("target_20_at"))]
        if value is not None
    ]
    mfe = [value for row in complete_paths_7d for value in [_float(row.get("path_mfe_7d"))] if value is not None]
    adverse = [-value for row in complete_paths_7d for value in [_float(row.get("path_mae_7d"))] if value is not None]
    adverse_before_20 = [
        -value
        for row in complete_paths_7d
        if _time_to_target_hours(row, 20, max_hours=168) is not None
        for value in [_float(row.get("path_mae_before_target_20"))]
        if value is not None
    ]
    mfe_times = [
        value
        for row in complete_paths_7d
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("path_mfe_at"))]
        if value is not None
    ]
    mae_times = [
        value
        for row in complete_paths_7d
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("path_mae_at"))]
        if value is not None
    ]

    baseline = BaselineSummary(
        total_signals=len(rows),
        matured_7d=len(matured),
        complete_paths_7d=len(complete_paths_7d),
        complete_paths_14d=len(complete_paths_14d),
        target_20_rate_7d=_rate(target_flags),
        positive_7d_rate=_rate(positive_flags),
        avg_return_7d=_mean(return_values),
        median_return_7d=_median(return_values),
        median_mfe_7d=_median(mfe),
        median_adverse_7d=_median(adverse),
        median_adverse_before_20=_median(adverse_before_20),
        median_time_to_20_hours=_median(target_times),
        median_time_to_mfe_hours=_median(mfe_times),
        median_time_to_mae_hours=_median(mae_times),
    )

    target_sweep: list[TargetSweepSummary] = []
    for target_pct in TARGET_LEVELS_PCT:
        times = [
            value
            for row in complete_paths_7d
            for value in [_time_to_target_hours(row, target_pct, max_hours=168)]
            if value is not None
        ]
        target_sweep.append(
            TargetSweepSummary(
                target_pct=target_pct,
                sample=len(complete_paths_7d),
                hits=len(times),
                hit_rate=(len(times) / len(complete_paths_7d)) if complete_paths_7d else None,
                median_time_hours=_median(times),
                p75_time_hours=_percentile(times, 0.75),
            )
        )

    slices: list[FeatureSliceSummary] = []
    for spec in FEATURE_SPECS:
        builder = _bool_feature_slices if spec.kind == "bool" else _numeric_feature_slices
        slices.extend(
            builder(
                matured,
                spec,
                baseline_target=baseline.target_20_rate_7d,
                baseline_positive=baseline.positive_7d_rate,
                baseline_avg_return=baseline.avg_return_7d,
            )
        )

    min_rank_sample = max(3, math.ceil(len(matured) * 0.15)) if matured else 3
    score_buckets = _build_score_buckets(matured)
    interactions = _build_interactions(
        matured,
        baseline_target=baseline.target_20_rate_7d,
        baseline_positive=baseline.positive_7d_rate,
        baseline_avg_return=baseline.avg_return_7d,
    )
    return ResearchAnalyticsReport(
        generated_at=generated_at,
        baseline=baseline,
        target_sweep=tuple(target_sweep),
        feature_slices=tuple(slices),
        standard_exit_sweep=_build_standard_exit_sweep(rows),
        high_risk_timeout_sweep=_build_high_risk_timeout_sweep(rows, generated_at=generated_at),
        stop_survival=_build_stop_survival(rows),
        score_buckets=score_buckets,
        interactions=interactions,
        delayed_entries=_build_delayed_entries(delayed_entry_rows),
        min_rank_sample=min_rank_sample,
    )


def _csv_pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100.0:.6f}"


def research_feature_lift_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "feature", "feature_label", "bucket", "sample",
            "target_20_rate_7d_pct", "positive_7d_rate_pct", "avg_return_7d_pct",
            "median_return_7d_pct", "median_mfe_7d_pct", "median_adverse_7d_pct",
            "target_lift_pp", "positive_lift_pp", "avg_return_lift_pp", "rank_score_pp",
        ]
    )
    for item in sorted(report.feature_slices, key=lambda x: (x.feature, x.bucket)):
        writer.writerow(
            [
                item.feature, item.feature_label, item.bucket, item.sample,
                _csv_pct(item.target_20_rate_7d), _csv_pct(item.positive_7d_rate),
                _csv_pct(item.avg_return_7d), _csv_pct(item.median_return_7d),
                _csv_pct(item.median_mfe_7d), _csv_pct(item.median_adverse_7d),
                _csv_pct(item.target_lift_pp), _csv_pct(item.positive_lift_pp),
                _csv_pct(item.avg_return_lift_pp), _csv_pct(item.rank_score),
            ]
        )
    return output.getvalue().encode("utf-8")


def research_strategy_sweeps_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "analysis", "risk_tier", "horizon_or_threshold", "sample",
        "metric_1", "metric_2", "metric_3", "metric_4", "metric_5", "metric_6",
    ])
    for item in report.standard_exit_sweep:
        writer.writerow([
            "standard_exit_horizon_paired", item.risk_tier, f"{item.horizon_hours}h", item.sample,
            _csv_pct(item.avg_return), _csv_pct(item.median_return), _csv_pct(item.positive_rate),
            _csv_pct(item.avg_return_per_day), f"cohort_complete_{item.cohort_horizon_hours}h", "",
        ])
    for item in report.high_risk_timeout_sweep:
        writer.writerow([
            "high_risk_tp20_timeout_mature_only", "high_risk", f"{item.timeout_hours}h", item.sample,
            _csv_pct(item.avg_strategy_return), _csv_pct(item.median_strategy_return),
            _csv_pct(item.target_hit_rate), _csv_pct(item.worst_strategy_return),
            "" if item.avg_holding_hours is None else f"{item.avg_holding_hours:.6f}",
            _csv_pct(item.return_per_slot_day),
        ])
    for item in report.stop_survival:
        writer.writerow([
            "winner_stop_survival", item.risk_tier, f"{item.stop_pct}%", item.winners_with_path,
            item.winners_killed, _csv_pct(item.kill_rate), _csv_pct(item.survivor_rate), "", "", "",
        ])
    return output.getvalue().encode("utf-8")


def research_entry_research_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["analysis", "name", "bucket_or_delay", "sample", "target_20_rate_7d_pct", "positive_7d_rate_pct", "avg_return_7d_pct", "median_return_7d_pct", "extra"])
    for item in report.score_buckets:
        writer.writerow([
            "shadow_score", item.score_name, item.bucket, item.sample,
            _csv_pct(item.target_20_rate_7d), _csv_pct(item.positive_7d_rate),
            _csv_pct(item.avg_return_7d), _csv_pct(item.median_return_7d), "",
        ])
    for item in report.interactions:
        writer.writerow([
            "feature_interaction", item.interaction, item.bucket, item.sample,
            _csv_pct(item.target_20_rate_7d), _csv_pct(item.positive_7d_rate),
            _csv_pct(item.avg_return_7d), "", _csv_pct(item.rank_score),
        ])
    for item in report.delayed_entries:
        writer.writerow([
            "delayed_entry_paired", "all_public_tiers", f"{item.delay_minutes}m", item.sample,
            _csv_pct(item.target_20_rate_7d), _csv_pct(item.positive_7d_rate),
            _csv_pct(item.avg_return_7d), _csv_pct(item.median_return_7d),
            _csv_pct(item.median_adverse_7d),
        ])
    return output.getvalue().encode("utf-8")


def research_signal_dataset_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    normalized = [dict(row) for row in rows]
    normalized = [row for row in normalized if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    fixed = [
        "episode_id", "symbol", "risk_tier", "confirmed_at", "entry_price",
        "return_24h_pct", "return_48h_pct", "return_72h_pct", "return_168h_pct",
        "target_20_at", "target_20_path_at", "path_rows", "path_last_at",
        "path_rows_7d", "path_rows_14d",
        "path_mfe_7d", "path_mae_7d", "path_mae_before_target_20", "path_mfe_at", "path_mae_at",
        "path_mfe_14d", "path_mae_14d", "path_mfe_14d_at", "path_mae_14d_at",
    ]
    horizon_fields = [f"path_return_{hours}h" for hours in STANDARD_EXIT_HORIZONS_HOURS]
    target_fields = [f"target_{pct}_at" for pct in TARGET_LEVELS_PCT if pct != 20]
    feature_fields = [spec.key for spec in FEATURE_SPECS]
    score_fields = ["shadow_entry_quality_score", "shadow_continuation_risk_score"]
    writer_fields = fixed + horizon_fields + target_fields + feature_fields + score_fields

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=writer_fields, extrasaction="ignore")
    writer.writeheader()
    for row in normalized:
        snapshot = json_object(row.get("feature_snapshot"))
        flattened: dict[str, Any] = {}
        for key in fixed + horizon_fields + target_fields:
            value = row.get(key)
            if isinstance(value, datetime):
                value = value.isoformat()
            flattened[key] = value
        for spec in FEATURE_SPECS:
            flattened[spec.key] = row.get(spec.key) if spec.source == "row" else snapshot.get(spec.key)
        quality, continuation = shadow_entry_scores(row)
        flattened["shadow_entry_quality_score"] = quality
        flattened["shadow_continuation_risk_score"] = continuation
        writer.writerow(flattened)
    return output.getvalue().encode("utf-8")
