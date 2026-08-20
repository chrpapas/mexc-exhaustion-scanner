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


@dataclass(frozen=True, slots=True)
class BaselineSummary:
    total_signals: int
    matured_7d: int
    complete_paths_7d: int
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
class ResearchAnalyticsReport:
    generated_at: datetime
    baseline: BaselineSummary
    target_sweep: tuple[TargetSweepSummary, ...]
    feature_slices: tuple[FeatureSliceSummary, ...]
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


def _target_within_7d(row: dict[str, Any]) -> bool:
    target = row.get("target_20_at")
    confirmed = row.get("confirmed_at")
    return bool(target is not None and confirmed is not None and target <= confirmed + timedelta(hours=168))


def _path_complete(row: dict[str, Any]) -> bool:
    confirmed = row.get("confirmed_at")
    last = row.get("path_last_at")
    if confirmed is None or last is None:
        return False
    # Allow one 15m candle of tolerance for alignment/gaps.
    return last >= confirmed + timedelta(hours=167, minutes=45)




def _elapsed_hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 3600.0)

def _time_to_target_hours(row: dict[str, Any], target_pct: int) -> float | None:
    key = "target_20_path_at" if target_pct == 20 else f"target_{target_pct}_at"
    hit = row.get(key)
    confirmed = row.get("confirmed_at")
    if hit is None or confirmed is None:
        return None
    return max(0.0, (hit - confirmed).total_seconds() / 3600.0)


def _slice_summary(
    rows: list[dict[str, Any]],
    *,
    spec: FeatureSpec,
    bucket: str,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> FeatureSliceSummary:
    target_rate = _rate([_target_within_7d(row) for row in rows])
    returns = [_float(row.get("return_168h_pct")) for row in rows]
    return_values = [value for value in returns if value is not None]
    positive_rate = _rate([value > 0 for value in return_values])
    avg_return = _mean(return_values)
    mfe = [_float(row.get("path_mfe_7d")) for row in rows if _path_complete(row)]
    adverse = [
        -value
        for row in rows
        if _path_complete(row)
        for value in [_float(row.get("path_mae_7d"))]
        if value is not None
    ]
    target_lift = target_rate - baseline_target if target_rate is not None and baseline_target is not None else None
    positive_lift = (
        positive_rate - baseline_positive
        if positive_rate is not None and baseline_positive is not None
        else None
    )
    avg_lift = (
        avg_return - baseline_avg_return
        if avg_return is not None and baseline_avg_return is not None
        else None
    )
    components = [value for value in (target_lift, positive_lift, avg_lift) if value is not None]
    rank_score = _mean(components)
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
        rank_score=rank_score,
    )


def _numeric_feature_slices(
    rows: list[dict[str, Any]],
    spec: FeatureSpec,
    *,
    baseline_target: float | None,
    baseline_positive: float | None,
    baseline_avg_return: float | None,
) -> list[FeatureSliceSummary]:
    valued: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        value = _float(_feature_value(row, spec))
        if value is not None:
            valued.append((row, value))
    if len(valued) < 3:
        return []
    values = [value for _, value in valued]
    q33 = _percentile(values, 1 / 3)
    q67 = _percentile(values, 2 / 3)
    if q33 is None or q67 is None or q33 == q67:
        return []

    buckets = (
        (f"LOW ≤ {q33:.4g}", [row for row, value in valued if value <= q33]),
        (f"MID {q33:.4g}–{q67:.4g}", [row for row, value in valued if q33 < value <= q67]),
        (f"HIGH > {q67:.4g}", [row for row, value in valued if value > q67]),
    )
    return [
        _slice_summary(
            bucket_rows,
            spec=spec,
            bucket=label,
            baseline_target=baseline_target,
            baseline_positive=baseline_positive,
            baseline_avg_return=baseline_avg_return,
        )
        for label, bucket_rows in buckets
        if bucket_rows
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
        if not group:
            continue
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


def build_research_analytics(
    raw_rows: Iterable[dict[str, Any]], *, generated_at: datetime
) -> ResearchAnalyticsReport:
    rows = [dict(row) for row in raw_rows]
    rows = [row for row in rows if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    matured = [row for row in rows if row.get("return_168h_pct") is not None]
    complete_paths = [row for row in matured if _path_complete(row)]

    returns = [_float(row.get("return_168h_pct")) for row in matured]
    return_values = [value for value in returns if value is not None]
    target_flags = [_target_within_7d(row) for row in matured]
    positive_flags = [value > 0 for value in return_values]
    target_times = [
        (row["target_20_at"] - row["confirmed_at"]).total_seconds() / 3600.0
        for row in matured
        if _target_within_7d(row)
    ]
    mfe = [
        value
        for row in complete_paths
        for value in [_float(row.get("path_mfe_7d"))]
        if value is not None
    ]
    adverse = [
        -value
        for row in complete_paths
        for value in [_float(row.get("path_mae_7d"))]
        if value is not None
    ]
    adverse_before_20 = [
        -value
        for row in complete_paths
        if row.get("target_20_at") is not None
        for value in [_float(row.get("path_mae_before_target_20"))]
        if value is not None
    ]
    mfe_times = [
        value
        for row in complete_paths
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("path_mfe_at"))]
        if value is not None
    ]
    mae_times = [
        value
        for row in complete_paths
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("path_mae_at"))]
        if value is not None
    ]

    baseline = BaselineSummary(
        total_signals=len(rows),
        matured_7d=len(matured),
        complete_paths_7d=len(complete_paths),
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
            for row in complete_paths
            for value in [_time_to_target_hours(row, target_pct)]
            if value is not None
        ]
        target_sweep.append(
            TargetSweepSummary(
                target_pct=target_pct,
                sample=len(complete_paths),
                hits=len(times),
                hit_rate=(len(times) / len(complete_paths)) if complete_paths else None,
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
    return ResearchAnalyticsReport(
        generated_at=generated_at,
        baseline=baseline,
        target_sweep=tuple(target_sweep),
        feature_slices=tuple(slices),
        min_rank_sample=min_rank_sample,
    )


def _csv_pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100.0:.6f}"


def research_feature_lift_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "feature",
            "feature_label",
            "bucket",
            "sample",
            "target_20_rate_7d_pct",
            "positive_7d_rate_pct",
            "avg_return_7d_pct",
            "median_return_7d_pct",
            "median_mfe_7d_pct",
            "median_adverse_7d_pct",
            "target_lift_pp",
            "positive_lift_pp",
            "avg_return_lift_pp",
            "rank_score_pp",
        ]
    )
    for item in sorted(report.feature_slices, key=lambda x: (x.feature, x.bucket)):
        writer.writerow(
            [
                item.feature,
                item.feature_label,
                item.bucket,
                item.sample,
                _csv_pct(item.target_20_rate_7d),
                _csv_pct(item.positive_7d_rate),
                _csv_pct(item.avg_return_7d),
                _csv_pct(item.median_return_7d),
                _csv_pct(item.median_mfe_7d),
                _csv_pct(item.median_adverse_7d),
                _csv_pct(item.target_lift_pp),
                _csv_pct(item.positive_lift_pp),
                _csv_pct(item.avg_return_lift_pp),
                _csv_pct(item.rank_score),
            ]
        )
    return output.getvalue().encode("utf-8")


def research_signal_dataset_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    normalized = [dict(row) for row in rows]
    normalized = [
        row
        for row in normalized
        if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS
    ]
    fixed = [
        "episode_id",
        "symbol",
        "risk_tier",
        "confirmed_at",
        "entry_price",
        "return_24h_pct",
        "return_48h_pct",
        "return_72h_pct",
        "return_168h_pct",
        "target_20_at",
        "target_20_path_at",
        "path_rows",
        "path_last_at",
        "path_mfe_7d",
        "path_mae_7d",
        "path_mae_before_target_20",
        "path_mfe_at",
        "path_mae_at",
    ]
    target_fields = [f"target_{pct}_at" for pct in TARGET_LEVELS_PCT if pct != 20]
    feature_fields = [spec.key for spec in FEATURE_SPECS]
    writer_fields = fixed + target_fields + feature_fields

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=writer_fields, extrasaction="ignore")
    writer.writeheader()
    for row in normalized:
        snapshot = json_object(row.get("feature_snapshot"))
        flattened: dict[str, Any] = {}
        for key in fixed + target_fields:
            value = row.get(key)
            if isinstance(value, datetime):
                value = value.isoformat()
            flattened[key] = value
        for spec in FEATURE_SPECS:
            value = row.get(spec.key) if spec.source == "row" else snapshot.get(spec.key)
            flattened[spec.key] = value
        writer.writerow(flattened)
    return output.getvalue().encode("utf-8")
