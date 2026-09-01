from __future__ import annotations

import csv
import io
import math
import statistics
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.json_utils import json_object
from app.trader_logic import (
    HTF_BASE_POSITION_FRACTION,
    HTF_FLAGGED_POSITION_FRACTION,
    htf_continuation_risk,
    PCR_BASE_POSITION_FRACTION,
    PCR_EMA_DISTANCE_ATR_THRESHOLD,
    PCR_FLAGGED_POSITION_FRACTION,
    PCR_RETURN_24H_THRESHOLD,
    parabolic_continuation_risk,
)
from app.token_regime import (
    EPISODIC_CLASS,
    INSUFFICIENT_CLASS,
    MIXED_CLASS,
    REGIME_FOLLOWER_CLASS,
    REGIME_LOOKBACK_DAYS,
    TokenRegimeResearchSummary,
    build_token_regime_research,
)

PUBLIC_RESEARCH_RISK_TIERS = frozenset({"standard", "high_risk"})
TARGET_LEVELS_PCT: tuple[int, ...] = (1, 2, 5, 10, 15, 20, 25, 30, 40)
STANDARD_EXIT_HORIZONS_HOURS: tuple[int, ...] = (24, 48, 72, 96, 120, 144, 168, 192, 240, 288, 336)
HIGH_RISK_TIMEOUT_HOURS: tuple[int, ...] = (24, 48, 72, 96, 120, 168, 240, 336)
STOP_THRESHOLDS_PCT: tuple[int, ...] = (10, 20, 30, 50, 75, 100)
DELAYED_ENTRY_MINUTES: tuple[int, ...] = (0, 15, 30, 60, 120, 240, 480)
TP5_ADVERSE_THRESHOLDS_PCT: tuple[int, ...] = (10, 20, 30, 50, 75, 100)
SHADOW_FEE_PER_FILL = 0.0008
CURRENT_TOTAL_EXPOSURE_PCT = 0.20
CURRENT_SLOT_PCT = CURRENT_TOTAL_EXPOSURE_PCT / 6.0
TP5_CHALLENGER_SLOT_PCT = 0.05
TP5_CHALLENGER_MAX_SLOTS = 6
TP5_SL75_STOP_PCT = 75
TP5_7D_CUTOFF_HOURS = 168
STRATEGY_TAIL_THRESHOLDS_PCT: tuple[int, ...] = (20, 50, 75, 100)
TP5_CHALLENGER_TOTAL_EXPOSURE_PCT = TP5_CHALLENGER_SLOT_PCT * TP5_CHALLENGER_MAX_SLOTS
FAST_TP_CHALLENGER_SLOT_PCT = 0.05
FAST_TP_CHALLENGER_MAX_SLOTS = 10
FAST_TP_CHALLENGER_TOTAL_EXPOSURE_PCT = FAST_TP_CHALLENGER_SLOT_PCT * FAST_TP_CHALLENGER_MAX_SLOTS
STANDARD_TP5_SCALE_SLOT_PCT = 0.05
STANDARD_TP5_SCALE_SLOT_PCT_75 = 0.075
STANDARD_TP5_SCALE_SLOT_PCT_100 = 0.10
STANDARD_TP5_SCALE_MAX_SLOTS = 10
STANDARD_TP5_SCALE_TOTAL_EXPOSURE_PCT = STANDARD_TP5_SCALE_SLOT_PCT * STANDARD_TP5_SCALE_MAX_SLOTS
# Research-only volatility-normalized sizing. The volatility anchor is frozen
# from the pre-OOS discovery cohort, then reused unchanged for post-freeze
# evaluation. Live/default trader sizing remains fixed at 5% / 6 slots / 30%.
VOLATILITY_BASE_SLOT_PCT = 0.05
VOLATILITY_MIN_SLOT_PCT = 0.025
VOLATILITY_MAX_SLOT_PCT = 0.075
VOLATILITY_MAX_SLOTS = 6
VOLATILITY_MAX_EXPOSURE_PCT = 0.30
# Frozen parabolic continuation-risk sizing rule. Thresholds were discovered/frozen
# in the 30 Aug 2026 PONS/HNT/CATE investigation and are now shared with live execution.
# Historical PCR replays through the freeze remain retrospective/hypothesis-generating.
PARABOLIC_RISK_RETURN_24H = PCR_RETURN_24H_THRESHOLD
PARABOLIC_RISK_EMA_DISTANCE_ATR = PCR_EMA_DISTANCE_ATR_THRESHOLD
PARABOLIC_RISK_POSITION_PCT = PCR_FLAGGED_POSITION_FRACTION
PARABOLIC_RISK_BASE_POSITION_PCT = PCR_BASE_POSITION_FRACTION
PARABOLIC_RISK_MAX_SLOTS = 6
PARABOLIC_RISK_MAX_EXPOSURE_PCT = 0.30
RESEARCH_OOS_FREEZE_AT = datetime(2026, 8, 21, 21, 29, tzinfo=UTC)
ENTRY_GATE_V1_MIN_QUALITY = 4
ENTRY_GATE_V1_MAX_CONTINUATION = 6
PROSPECTIVE_GATE_ROLLING_WINDOW = 20
CALENDAR_MONTH_DAYS = 30

# Research-only persistent-pump continuation-risk candidate. These thresholds
# were selected from the 26 Aug 2026 analysis and are frozen prospectively;
# they MUST NOT affect subscriber strategy eligibility or TP5 execution.
PERSISTENT_RUN_RISK_FREEZE_AT = datetime(2026, 8, 26, 13, 22, tzinfo=UTC)
PERSISTENT_RUN_RISK_OBSERVATION_HOURS = 120
PERSISTENT_RUN_LONG_HOURS = 36.0
PERSISTENT_RUN_MAX_EMA_DISTANCE_ATR = 3.0
PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF = (
    PERSISTENT_RUN_RISK_FREEZE_AT - timedelta(hours=PERSISTENT_RUN_RISK_OBSERVATION_HOURS)
)


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
    FeatureSpec("atr_15m_pct", "15m ATR14 / entry price", source="derived"),
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
    ("atr_15m_pct", "distance_above_ema20_atr_4h"),
    ("atr_15m_pct", "run_score"),
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
    wins: int = 0
    losses: int = 0
    sum_strategy_return: float | None = None
    best_strategy_return: float | None = None


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
class Tp5AdverseRaceSummary:
    adverse_threshold_pct: int
    sample: int
    target_first: int
    adverse_first: int
    same_candle: int
    unresolved: int
    target_first_rate: float | None


@dataclass(frozen=True, slots=True)
class Tp5RiskSummary:
    sample: int
    hits: int
    hit_rate: float | None
    median_time_hours: float | None
    p75_time_hours: float | None
    median_adverse_before_target: float | None
    p75_adverse_before_target: float | None
    worst_adverse_before_target: float | None
    adverse_races: tuple[Tp5AdverseRaceSummary, ...]


@dataclass(frozen=True, slots=True)
class StrategyTailSummary:
    threshold_pct: int
    breached_before_exit_or_mark: int
    breach_rate: float | None
    later_tp5_after_breach: int


@dataclass(frozen=True, slots=True)
class StrategyValidationSummary:
    strategy: str
    label: str
    rule: str
    sample: int
    resolved: int
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
    target_rate_to_date: float | None
    avg_exit_return: float | None
    median_exit_return: float | None
    best_exit_return: float | None
    worst_exit_return: float | None
    median_holding_hours: float | None
    p75_holding_hours: float | None
    tail_ladder: tuple[StrategyTailSummary, ...]


@dataclass(frozen=True, slots=True)
class PortfolioReplaySummary:
    strategy: str
    cohort: str
    signals: int
    eligible_signals: int
    filtered_entry_gate: int
    entered: int
    closed: int
    open_positions: int
    missed_capacity: int
    missed_same_symbol: int
    realized_return: float
    marked_return: float
    max_open_positions: int
    max_observed_exposure_pct: float
    median_holding_hours: float | None
    avg_holding_hours: float | None
    slot_days: float
    return_per_slot_day: float | None
    replay_span_days: float | None
    unmarked_open_positions: int
    mtm_points: int
    max_mtm_drawdown: float | None
    worst_mtm_return: float | None
    max_unrealized_loss: float | None
    max_simultaneous_losers: int
    avg_exposure_pct: float | None
    p95_exposure_pct: float | None
    avg_open_positions: float | None
    drawdown_recovery_hours: float | None
    return_over_max_drawdown: float | None
    worst_trade_episode_id: int | None
    worst_trade_pre_target_mae: float | None
    portfolio_return_at_worst_trade_mae: float | None
    filtered_strategy: int = 0


@dataclass(frozen=True, slots=True)
class ProspectiveCohortSummary:
    cohort: str
    signal_cutoff: datetime
    signals: int
    complete_7d: int
    tp5_hits: int
    tp5_hit_rate: float | None
    median_tp5_hours: float | None
    worst_pre_tp5_adverse: float | None
    entrygate_eligible: int
    entrygate_eligible_rate: float | None
    entrygate_complete_7d: int


@dataclass(frozen=True, slots=True)
class ProspectiveTp5LiveSummary:
    signals: int
    hits: int
    waiting: int
    waiting_over_7d: int
    observed_hit_rate: float | None
    median_hit_hours: float | None
    p75_hit_hours: float | None
    worst_pre_hit_adverse: float | None
    worst_waiting_close_adverse: float | None
    oldest_waiting_hours: float | None


@dataclass(frozen=True, slots=True)
class ProspectiveGateAcceptanceSummary:
    signals: int
    eligible: int
    eligible_rate: float | None
    rolling_window: int
    rolling_signals: int
    rolling_eligible: int
    rolling_eligible_rate: float | None


@dataclass(frozen=True, slots=True)
class VolatilityBucketSummary:
    cohort: str
    risk_tier: str
    bucket: str
    sample: int
    atr_pct_min: float | None
    atr_pct_max: float | None
    target_exits: int
    stop_exits: int
    waiting: int
    target_rate_to_date: float | None
    median_tp5_hours: float | None
    avg_marked_return: float | None
    breach20_rate: float | None
    breach50_rate: float | None
    breach75_rate: float | None
    breach100_rate: float | None


@dataclass(frozen=True, slots=True)
class VolatilityResearchSummary:
    freeze_at: datetime
    calibration_sample: int
    observed_sample: int
    missing_atr: int
    calibration_p25: float | None
    calibration_median: float | None
    calibration_p75: float | None
    size_floor: float
    size_base: float
    size_ceiling: float
    max_slots: int
    max_exposure: float
    buckets: tuple[VolatilityBucketSummary, ...]
    portfolio_fixed: PortfolioReplaySummary
    portfolio_normalized: PortfolioReplaySummary
    prospective_portfolio_fixed: PortfolioReplaySummary
    prospective_portfolio_normalized: PortfolioReplaySummary
    parabolic_return_24h_threshold: float
    parabolic_ema_distance_atr_threshold: float
    parabolic_position_fraction: float
    parabolic_flagged_validation: StrategyValidationSummary
    parabolic_unflagged_validation: StrategyValidationSummary
    parabolic_portfolio_de_risked: PortfolioReplaySummary
    prospective_parabolic_flagged_validation: StrategyValidationSummary
    prospective_parabolic_portfolio_de_risked: PortfolioReplaySummary
    htf_computable_signals: int
    htf_missing_signals: int
    htf_flagged_validation: StrategyValidationSummary
    htf_unflagged_validation: StrategyValidationSummary
    htf_portfolio_de_risked: PortfolioReplaySummary


@dataclass(frozen=True, slots=True)
class RegimeDriftSummary:
    feature: str
    feature_label: str
    discovery_sample: int
    post_freeze_sample: int
    discovery_median: float | None
    post_freeze_median: float | None
    discovery_p25: float | None
    discovery_p75: float | None
    post_below_discovery_p25_rate: float | None
    post_inside_discovery_iqr_rate: float | None
    post_above_discovery_p75_rate: float | None


@dataclass(frozen=True, slots=True)
class CalendarThroughputComparison:
    history_start: datetime | None
    history_end: datetime
    history_span_days: float
    current: PortfolioReplaySummary
    tp5: PortfolioReplaySummary
    tp5_sl75: PortfolioReplaySummary
    hold_7d: PortfolioReplaySummary
    tp2: PortfolioReplaySummary
    tp2_10: PortfolioReplaySummary
    tp1_10: PortfolioReplaySummary
    latest_30d_current: PortfolioReplaySummary | None
    latest_30d_tp5: PortfolioReplaySummary | None
    latest_30d_tp5_sl75: PortfolioReplaySummary | None
    latest_30d_hold_7d: PortfolioReplaySummary | None
    latest_30d_tp2: PortfolioReplaySummary | None
    latest_30d_tp2_10: PortfolioReplaySummary | None
    latest_30d_tp1_10: PortfolioReplaySummary | None
    latest_30d_start: datetime | None
    days_until_30d: float


@dataclass(frozen=True, slots=True)
class PersistentRunRiskBucketSummary:
    cohort: str
    flag_name: str
    flagged: bool
    signals: int
    evaluable_120h: int
    adverse_100_breaches: int
    adverse_100_rate: float | None
    tp5_before_adverse_100: int


@dataclass(frozen=True, slots=True)
class PersistentRunRiskResearchSummary:
    freeze_at: datetime
    nonbreach_maturity_cutoff: datetime
    observation_hours: int
    long_run_hours: float
    max_ema_distance_atr: float
    buckets: tuple[PersistentRunRiskBucketSummary, ...]

    def bucket(self, cohort: str, flag_name: str, flagged: bool) -> PersistentRunRiskBucketSummary | None:
        return next(
            (
                item for item in self.buckets
                if item.cohort == cohort and item.flag_name == flag_name and item.flagged is flagged
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class CohortScoreBucketSummary:
    cohort: str
    score_name: str
    bucket: str
    sample: int
    target_20_rate_7d: float | None
    positive_7d_rate: float | None
    avg_return_7d: float | None


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
    tp5_risk: Tp5RiskSummary
    strategy_validations: tuple[StrategyValidationSummary, ...]
    portfolio_current: PortfolioReplaySummary
    portfolio_tp5: PortfolioReplaySummary
    portfolio_tp5_sl75: PortfolioReplaySummary
    portfolio_hold_7d: PortfolioReplaySummary
    standard_tp5_validation: StrategyValidationSummary
    standard_tp5_sl75_validation: StrategyValidationSummary
    portfolio_standard_tp5_10: PortfolioReplaySummary
    portfolio_standard_tp5_10x75: PortfolioReplaySummary
    portfolio_standard_tp5_10x10: PortfolioReplaySummary
    portfolio_standard_tp5_sl75_10: PortfolioReplaySummary
    portfolio_standard_tp5_sl75_10x10: PortfolioReplaySummary
    portfolio_entrygate_current: PortfolioReplaySummary
    portfolio_entrygate_tp5: PortfolioReplaySummary
    prospective_cohorts: tuple[ProspectiveCohortSummary, ...]
    prospective_score_buckets: tuple[CohortScoreBucketSummary, ...]
    prospective_tp5_live: ProspectiveTp5LiveSummary
    prospective_strategy_validations: tuple[StrategyValidationSummary, ...]
    prospective_strategy_portfolios: tuple[PortfolioReplaySummary, ...]
    prospective_standard_tp5_validation: StrategyValidationSummary
    prospective_standard_tp5_sl75_validation: StrategyValidationSummary
    prospective_portfolio_standard_tp5_10: PortfolioReplaySummary
    prospective_portfolio_standard_tp5_10x75: PortfolioReplaySummary
    prospective_portfolio_standard_tp5_10x10: PortfolioReplaySummary
    prospective_portfolio_standard_tp5_sl75_10: PortfolioReplaySummary
    prospective_portfolio_standard_tp5_sl75_10x10: PortfolioReplaySummary
    prospective_gate_acceptance: ProspectiveGateAcceptanceSummary
    prospective_regime_drift: tuple[RegimeDriftSummary, ...]
    prospective_portfolios: tuple[PortfolioReplaySummary, ...]
    calendar_throughput: CalendarThroughputComparison
    token_regime: TokenRegimeResearchSummary
    regime_portfolios: tuple[PortfolioReplaySummary, ...]
    hybrid_portfolios: tuple[PortfolioReplaySummary, ...]
    persistent_run_risk: PersistentRunRiskResearchSummary
    volatility: VolatilityResearchSummary
    oos_freeze_at: datetime
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


def _entry_atr_pct(row: dict[str, Any]) -> float | None:
    snapshot = row.get("_snapshot")
    if snapshot is None:
        snapshot = json_object(row.get("feature_snapshot"))
        row["_snapshot"] = snapshot
    atr = _float(snapshot.get("atr_15m"))
    price = _float(row.get("entry_price"))
    if atr is None or price is None or atr <= 0 or price <= 0:
        return None
    return atr / price


def _feature_value(row: dict[str, Any], spec: FeatureSpec) -> Any:
    if spec.source == "row":
        return row.get(spec.key)
    if spec.source == "derived":
        if spec.key == "atr_15m_pct":
            return _entry_atr_pct(row)
        return None
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
    # Intermediate horizons do not always have their own row-count column in the
    # flattened research dataset. The cumulative 14d count is authoritative for
    # horizons >7d; likewise the 7d count can cover shorter horizons.
    if observed is None and hours > 168:
        observed = _float(row.get("path_rows_14d"))
    elif observed is None and hours < 168:
        observed = _float(row.get("path_rows_7d"))
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


def entry_gate_v1(row: dict[str, Any]) -> bool:
    """Frozen shadow-only entry gate defined at the v1.3.3 research freeze.

    It intentionally reuses the already-frozen Entry Quality / Continuation Risk
    score boundaries. It never changes scanner classification or live trader entry.
    """
    quality, continuation = shadow_entry_scores(row)
    return quality >= ENTRY_GATE_V1_MIN_QUALITY and continuation <= ENTRY_GATE_V1_MAX_CONTINUATION


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
    """Simulate TP20-or-timeout on paired mature High-Risk cohorts.

    Timeouts through 10d use the exact same 10d-mature cohort, so 1d/2d/3d/4d/
    5d/7d/10d can be compared directly. A row belongs to that cohort only when every
    timeout is evaluable: either +20% was already hit by that timeout or a timeout
    return exists. The 14d row uses its own fully mature/evaluable 14d cohort.
    """
    risky = [row for row in rows if str(row.get("risk_tier") or "standard") == "high_risk"]
    paired_hours = tuple(h for h in HIGH_RISK_TIMEOUT_HOURS if h <= 240)

    # Strict paired cohorts: every 1d..10d row must come from the exact same
    # episodes with a complete stored 10-day path. Do not use broad shadow
    # snapshots here because those can exist for some horizons but not others
    # and would silently change the denominator between timeout rows.
    cohort_10d = [
        row for row in risky
        if _eligible_at_timeout(row, 240, generated_at)
        and _path_complete_for_hours(row, 240)
        and all(_float(row.get(f"path_return_{hours}h")) is not None for hours in paired_hours)
    ]
    cohort_14d = [
        row for row in risky
        if _eligible_at_timeout(row, 336, generated_at)
        and _path_complete_for_hours(row, 336)
        and _float(row.get("path_return_336h")) is not None
    ]

    result: list[HighRiskTimeoutSummary] = []
    for hours in HIGH_RISK_TIMEOUT_HOURS:
        cohort = cohort_10d if hours <= 240 else cohort_14d
        outcomes: list[float] = []
        holding_hours: list[float] = []
        target_hits = 0
        for row in cohort:
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
            timeout_return = _float(row.get(f"path_return_{hours}h"))
            if timeout_return is None:
                # Strict paired cohort invariant: every included episode must have
                # every timeout return required for the comparison.
                outcomes = []
                holding_hours = []
                target_hits = 0
                break
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
                wins=sum(value > 0 for value in outcomes),
                losses=sum(value <= 0 for value in outcomes),
                sum_strategy_return=sum(outcomes) if outcomes else None,
                best_strategy_return=max(outcomes) if outcomes else None,
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


def _tp5_risk_summary(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    max_hours: int | None = None,
) -> Tp5RiskSummary:
    """Summarize TP5 outcomes over the requested observation window.

    ``max_hours=None`` is the live TP5 semantics: a position remains open until
    +5% is hit, regardless of age. Fixed-horizon research can still pass a
    finite ``max_hours`` (for example 168h) to answer a separate question such
    as "what fraction hit TP5 within seven days?".
    """

    def bounded_event(row: dict[str, Any], key: str) -> datetime | None:
        event = row.get(key)
        confirmed = row.get("confirmed_at")
        if event is None or confirmed is None:
            return None
        if generated_at is not None and event > generated_at:
            return None
        if max_hours is not None and event > confirmed + timedelta(hours=max_hours):
            return None
        return event

    sample = len(rows)
    hit_rows: list[dict[str, Any]] = []
    times: list[float] = []
    for row in rows:
        target = bounded_event(row, "target_5_at")
        elapsed = _elapsed_hours(row.get("confirmed_at"), target)
        if target is not None and elapsed is not None:
            hit_rows.append(row)
            times.append(elapsed)
    adverse_before: list[float] = []
    for row in hit_rows:
        # Older exported datasets do not contain the v1.3.2 pre-TP5 field at all.
        # Missing column means unknown; a present SQL NULL means there was no
        # earlier post-signal candle before a first-candle +5% hit, which is 0
        # observed pre-hit adverse excursion.
        if "path_mae_before_target_5" not in row:
            continue
        raw = _float(row.get("path_mae_before_target_5"))
        adverse_before.append(max(0.0, -raw) if raw is not None else 0.0)

    races: list[Tp5AdverseRaceSummary] = []
    for threshold in TP5_ADVERSE_THRESHOLDS_PCT:
        target_first = adverse_first = same_candle = unresolved = 0
        for row in rows:
            confirmed = row.get("confirmed_at")
            if confirmed is None:
                unresolved += 1
                continue
            target = bounded_event(row, "target_5_at")
            adverse_key = f"adverse_{threshold}_at"
            if adverse_key not in row:
                unresolved += 1
                continue
            adverse = bounded_event(row, adverse_key)
            if target is not None and (adverse is None or target < adverse):
                target_first += 1
            elif adverse is not None and (target is None or adverse < target):
                adverse_first += 1
            elif target is not None and adverse is not None and target == adverse:
                same_candle += 1
            else:
                unresolved += 1
        races.append(
            Tp5AdverseRaceSummary(
                adverse_threshold_pct=threshold,
                sample=sample,
                target_first=target_first,
                adverse_first=adverse_first,
                same_candle=same_candle,
                unresolved=unresolved,
                target_first_rate=(target_first / sample) if sample else None,
            )
        )

    return Tp5RiskSummary(
        sample=sample,
        hits=len(hit_rows),
        hit_rate=(len(hit_rows) / sample) if sample else None,
        median_time_hours=_median(times),
        p75_time_hours=_percentile(times, 0.75),
        median_adverse_before_target=_median(adverse_before),
        p75_adverse_before_target=_percentile(adverse_before, 0.75),
        worst_adverse_before_target=max(adverse_before) if adverse_before else None,
        adverse_races=tuple(races),
    )


def _first_target_20(row: dict[str, Any]) -> datetime | None:
    values = [
        value
        for value in (row.get("target_20_path_at"), row.get("target_20_at"))
        if value is not None
    ]
    return min(values) if values else None


def _latest_observed_return(row: dict[str, Any]) -> float | None:
    direct = _float(row.get("path_latest_return"))
    if direct is not None:
        return direct
    for hours in reversed(STANDARD_EXIT_HORIZONS_HOURS):
        value = _float(row.get(f"path_return_{hours}h"))
        if value is not None:
            return value
    for key in ("return_168h_pct", "return_72h_pct", "return_48h_pct", "return_24h_pct"):
        value = _float(row.get(key))
        if value is not None:
            return value
    return _float(row.get("current_return_pct"))


def _strategy_outcome(
    row: dict[str, Any], *, strategy: str, generated_at: datetime
) -> tuple[str, datetime | None, float | None]:
    """Resolve one signal under the three trader-facing TP5 strategies.

    The strategies share the same confirmed-short entry stream.  The only
    difference is exit policy:
      * tp5_challenger: +5% target, otherwise remain open indefinitely.
      * tp5_sl75_challenger: +5% target or -75% catastrophic stop, no timeout.
      * hold_7d: hold every entered short for exactly 168h, then close at
        the observed 168h return. There is no profit target and no stop.

    Same-candle target/SL75 races are conservatively stop-first.
    """
    confirmed = row.get("confirmed_at")
    if confirmed is None or confirmed > generated_at:
        return "waiting", None, None

    target = row.get("target_5_at")
    target_observed = target is not None and target <= generated_at

    if strategy == "tp5_challenger":
        if target_observed:
            return "target", target, 0.05
        return "waiting", None, None

    if strategy == "tp5_sl75_challenger":
        stop = row.get(f"adverse_{TP5_SL75_STOP_PCT}_at")
        stop_observed = stop is not None and stop <= generated_at
        if stop_observed and (not target_observed or stop <= target):
            return "stop", stop, -(TP5_SL75_STOP_PCT / 100.0)
        if target_observed:
            return "target", target, 0.05
        return "waiting", None, None

    if strategy == "hold_7d":
        cutoff = confirmed + timedelta(hours=TP5_7D_CUTOFF_HOURS)
        if cutoff <= generated_at:
            value = _horizon_return(row, TP5_7D_CUTOFF_HOURS)
            if value is not None:
                return "timeout", cutoff, value
        return "waiting", None, None

    raise ValueError(f"unsupported validation strategy: {strategy}")


def _strategy_validation_summary(
    rows: list[dict[str, Any]], *, strategy: str, generated_at: datetime
) -> StrategyValidationSummary:
    labels = {
        "tp5_challenger": ("TP5 indefinite", "+5% target • no stop • no timeout"),
        "tp5_sl75_challenger": ("TP5 + SL75", "+5% target • -75% catastrophic stop • no timeout"),
        "hold_7d": ("7D hold", "hold exactly 168h • close at the 7D return • no TP / no SL"),
    }
    label, rule = labels[strategy]
    observed = [
        row for row in rows
        if row.get("confirmed_at") is not None and row["confirmed_at"] <= generated_at
    ]
    statuses: list[str] = []
    exits: list[float] = []
    marked_returns: list[float] = []
    holds: list[float] = []
    effective_ends: list[tuple[dict[str, Any], datetime]] = []

    for row in observed:
        status, exit_at, exit_return = _strategy_outcome(
            row, strategy=strategy, generated_at=generated_at
        )
        statuses.append(status)
        if exit_at is not None and exit_return is not None:
            exits.append(float(exit_return))
            marked_returns.append(float(exit_return) - (2.0 * SHADOW_FEE_PER_FILL))
            hold = _elapsed_hours(row.get("confirmed_at"), exit_at)
            if hold is not None:
                holds.append(hold)
            effective_ends.append((row, exit_at))
        else:
            mark = _latest_observed_return(row)
            if mark is not None:
                marked_returns.append(float(mark) - (2.0 * SHADOW_FEE_PER_FILL))
            effective_ends.append((row, generated_at))

    tails: list[StrategyTailSummary] = []
    for threshold in STRATEGY_TAIL_THRESHOLDS_PCT:
        breached = 0
        later_tp5 = 0
        key = f"adverse_{threshold}_at"
        for row, effective_end in effective_ends:
            event = row.get(key)
            if event is None or event > effective_end:
                continue
            breached += 1
            target = row.get("target_5_at")
            if target is not None and target > event and target <= generated_at:
                later_tp5 += 1
        tails.append(
            StrategyTailSummary(
                threshold_pct=threshold,
                breached_before_exit_or_mark=breached,
                breach_rate=(breached / len(observed)) if observed else None,
                later_tp5_after_breach=later_tp5,
            )
        )

    resolved = sum(status != "waiting" for status in statuses)
    target_exits = statuses.count("target")
    stop_exits = statuses.count("stop")
    timeout_exits = statuses.count("timeout")
    positive = sum(value > 0 for value in exits)
    negative = sum(value <= 0 for value in exits)
    return StrategyValidationSummary(
        strategy=strategy,
        label=label,
        rule=rule,
        sample=len(observed),
        resolved=resolved,
        target_exits=target_exits,
        stop_exits=stop_exits,
        timeout_exits=timeout_exits,
        waiting=statuses.count("waiting"),
        marked_sample=len(marked_returns),
        marked_positive_rate=(sum(value > 0 for value in marked_returns) / len(marked_returns)) if marked_returns else None,
        avg_marked_return=_mean(marked_returns),
        median_marked_return=_median(marked_returns),
        sum_marked_return=sum(marked_returns) if marked_returns else None,
        positive_exits=positive,
        negative_exits=negative,
        resolved_positive_rate=(positive / resolved) if resolved else None,
        target_rate_to_date=(target_exits / len(observed)) if observed else None,
        avg_exit_return=_mean(exits),
        median_exit_return=_median(exits),
        best_exit_return=max(exits) if exits else None,
        worst_exit_return=min(exits) if exits else None,
        median_holding_hours=_median(holds),
        p75_holding_hours=_percentile(holds, 0.75),
        tail_ladder=tuple(tails),
    )


def _known_exit(
    row: dict[str, Any], *, strategy: str, generated_at: datetime, target_pct_override: int | None = None
) -> tuple[datetime | None, float | None]:
    confirmed = row.get("confirmed_at")
    if confirmed is None:
        return None, None
    if strategy == "tp5_challenger" and target_pct_override is not None:
        target = row.get(f"target_{target_pct_override}_at")
        if target is not None and target <= generated_at:
            return target, target_pct_override / 100.0
        return None, None
    if strategy in {"tp5_challenger", "tp5_sl75_challenger", "hold_7d"}:
        _, exit_at, exit_return = _strategy_outcome(row, strategy=strategy, generated_at=generated_at)
        return exit_at, exit_return
    if strategy in {"tp2_challenger", "tp2_10_challenger", "tp1_10_challenger"}:
        target_pct = target_pct_override if target_pct_override is not None else {
            "tp2_challenger": 2,
            "tp2_10_challenger": 2,
            "tp1_10_challenger": 1,
        }[strategy]
        target = row.get(f"target_{target_pct}_at")
        if target is not None and target <= generated_at:
            return target, target_pct / 100.0
        return None, None

    tier = str(row.get("risk_tier") or "standard")
    if tier == "standard":
        exit_at = confirmed + timedelta(hours=168)
        if exit_at > generated_at:
            return None, None
        value = _horizon_return(row, 168)
        return (exit_at, value) if value is not None else (None, None)

    if tier == "high_risk":
        timeout = confirmed + timedelta(hours=96)
        target = _first_target_20(row)
        if target is not None and target <= timeout and target <= generated_at:
            return target, 0.20
        if timeout <= generated_at:
            value = _float(row.get("path_return_96h"))
            if value is None:
                value = _horizon_return(row, 96)
            return (timeout, value) if value is not None else (None, None)
    return None, None


def _portfolio_mtm_metrics(
    accepted: list[dict[str, Any]],
    *,
    path_rows: Iterable[dict[str, Any]],
    position_fraction: float | None = None,
) -> dict[str, Any]:
    if not accepted:
        return {
            "mtm_points": 0,
            "max_mtm_drawdown": None,
            "worst_mtm_return": None,
            "max_unrealized_loss": None,
            "max_simultaneous_losers": 0,
            "avg_exposure_pct": None,
            "p95_exposure_pct": None,
            "avg_open_positions": None,
            "drawdown_recovery_hours": None,
            "worst_trade_episode_id": None,
            "worst_trade_pre_target_mae": None,
            "portfolio_return_at_worst_trade_mae": None,
        }

    accepted_by_episode = {p["episode_id"]: p for p in accepted if p.get("episode_id") is not None}
    path_by_time: dict[datetime, list[tuple[int, float]]] = {}
    for raw in path_rows:
        episode_id = raw.get("episode_id")
        pos = accepted_by_episode.get(episode_id)
        at = raw.get("candle_close_at")
        value = _float(raw.get("close_return_pct"))
        if pos is None or at is None or value is None:
            continue
        if not (pos["entry_at"] < at <= (pos["exit_at"] or pos["mark_until"])):
            continue
        path_by_time.setdefault(at, []).append((int(episode_id), value))

    entries_by_time: dict[datetime, list[dict[str, Any]]] = {}
    exits_by_time: dict[datetime, list[dict[str, Any]]] = {}
    all_times: set[datetime] = set(path_by_time)
    for pos in accepted:
        entries_by_time.setdefault(pos["entry_at"], []).append(pos)
        all_times.add(pos["entry_at"])
        if pos["exit_at"] is not None:
            exits_by_time.setdefault(pos["exit_at"], []).append(pos)
            all_times.add(pos["exit_at"])

    realized_equity = 1.0
    open_positions: dict[int, dict[str, Any]] = {}
    latest_marks: dict[int, float] = {}
    peak_equity = 1.0
    peak_at: datetime | None = None
    underwater_since: datetime | None = None
    max_recovery_hours = 0.0
    max_drawdown = 0.0
    worst_return = 0.0
    max_unrealized_loss = 0.0
    max_losers = 0
    exposures: list[float] = []
    occupancies: list[float] = []
    trace: list[tuple[datetime, float]] = []

    # Reconstruct the replay cash/equity state with the exact notionals chosen by
    # _portfolio_replay. Exit events are processed before same-timestamp entries,
    # matching the chronological slot-recycling semantics used by the replay.
    for at in sorted(all_times):
        for episode_id, mark in path_by_time.get(at, []):
            if episode_id in open_positions:
                latest_marks[episode_id] = mark

        for pos in exits_by_time.get(at, []):
            episode_id = int(pos["episode_id"])
            if episode_id not in open_positions:
                continue
            realized_equity += pos["notional"] * (float(pos["exit_return"]) - SHADOW_FEE_PER_FILL)
            open_positions.pop(episode_id, None)
            latest_marks.pop(episode_id, None)

        for pos in entries_by_time.get(at, []):
            episode_id = int(pos["episode_id"])
            realized_equity -= pos["notional"] * SHADOW_FEE_PER_FILL
            open_positions[episode_id] = pos
            latest_marks[episode_id] = 0.0

        unrealized = sum(
            float(pos["notional"]) * latest_marks.get(episode_id, 0.0)
            for episode_id, pos in open_positions.items()
        )
        marked_equity = realized_equity + unrealized
        trace.append((at, marked_equity - 1.0))
        worst_return = min(worst_return, marked_equity - 1.0)
        max_unrealized_loss = max(max_unrealized_loss, max(0.0, -unrealized))
        losers = sum(1 for episode_id in open_positions if latest_marks.get(episode_id, 0.0) < 0)
        max_losers = max(max_losers, losers)
        occupancies.append(float(len(open_positions)))
        exposures.append(sum(
            float(pos.get("position_fraction") or position_fraction or 0.0)
            for pos in open_positions.values()
        ))

        if marked_equity >= peak_equity:
            if underwater_since is not None:
                max_recovery_hours = max(
                    max_recovery_hours,
                    max(0.0, (at - underwater_since).total_seconds() / 3600.0),
                )
                underwater_since = None
            peak_equity = marked_equity
            peak_at = at
        else:
            if underwater_since is None:
                underwater_since = peak_at or at
            if peak_equity > 0:
                max_drawdown = max(max_drawdown, (peak_equity - marked_equity) / peak_equity)

    if underwater_since is not None and all_times:
        max_recovery_hours = max(
            max_recovery_hours,
            max(0.0, (max(all_times) - underwater_since).total_seconds() / 3600.0),
        )

    worst_pos: dict[str, Any] | None = None
    for pos in accepted:
        raw = _float(pos["row"].get("path_mae_before_target_5"))
        at = pos["row"].get("path_mae_before_target_5_at")
        if raw is None or at is None:
            continue
        if worst_pos is None or raw < float(worst_pos["raw_mae"]):
            worst_pos = {"pos": pos, "raw_mae": raw, "at": at}

    portfolio_at_worst = None
    if worst_pos is not None and trace:
        target_at = worst_pos["at"]
        eligible_trace = [item for item in trace if item[0] <= target_at]
        if eligible_trace:
            portfolio_at_worst = eligible_trace[-1][1]

    return {
        "mtm_points": len(trace),
        "max_mtm_drawdown": max_drawdown if trace else None,
        "worst_mtm_return": worst_return if trace else None,
        "max_unrealized_loss": max_unrealized_loss if trace else None,
        "max_simultaneous_losers": max_losers,
        "avg_exposure_pct": _mean(exposures),
        "p95_exposure_pct": _percentile(exposures, 0.95),
        "avg_open_positions": _mean(occupancies),
        "drawdown_recovery_hours": max_recovery_hours if trace else None,
        "worst_trade_episode_id": (
            int(worst_pos["pos"]["episode_id"]) if worst_pos is not None else None
        ),
        "worst_trade_pre_target_mae": (
            max(0.0, -float(worst_pos["raw_mae"])) if worst_pos is not None else None
        ),
        "portfolio_return_at_worst_trade_mae": portfolio_at_worst,
    }


def _portfolio_replay(
    rows: list[dict[str, Any]],
    *,
    strategy: str,
    generated_at: datetime,
    path_rows: Iterable[dict[str, Any]] = (),
    use_entry_gate: bool = False,
    cohort: str = "paired_complete_7d",
    eligible_episode_ids: set[int] | None = None,
    strategy_name_override: str | None = None,
    priority_scores: dict[int, float] | None = None,
    target_pct_by_episode: dict[int, int] | None = None,
    position_fraction_override: float | None = None,
    max_total_override: int | None = None,
    position_fraction_by_episode: dict[int, float] | None = None,
    max_exposure_fraction_override: float | None = None,
) -> PortfolioReplaySummary:
    candidates = [
        row for row in rows
        if row.get("confirmed_at") is not None and row.get("confirmed_at") <= generated_at
    ]

    def order_key(row: dict[str, Any]) -> tuple[datetime, float]:
        confirmed_at = row["confirmed_at"]
        if not priority_scores:
            return confirmed_at, 0.0
        episode_id = row.get("episode_id")
        score = priority_scores.get(int(episode_id), -1.0) if episode_id is not None else -1.0
        return confirmed_at, -score

    ordered = sorted(candidates, key=order_key)
    if strategy in {"tp5_challenger", "tp5_sl75_challenger", "hold_7d", "tp2_challenger", "tp2_10_challenger", "tp1_10_challenger"}:
        if strategy in {"tp2_10_challenger", "tp1_10_challenger"}:
            position_fraction = FAST_TP_CHALLENGER_SLOT_PCT
            max_total = FAST_TP_CHALLENGER_MAX_SLOTS
        else:
            position_fraction = TP5_CHALLENGER_SLOT_PCT
            max_total = TP5_CHALLENGER_MAX_SLOTS
        max_standard = max_high = None
        base_name = {
            "tp5_challenger": "tp5_challenger_6x5pct",
            "tp5_sl75_challenger": "tp5_sl75_challenger_6x5pct",
            "hold_7d": "hold_7d_6x5pct",
            "tp2_challenger": "tp2_challenger_6x5pct",
            "tp2_10_challenger": "tp2_challenger_10x5pct",
            "tp1_10_challenger": "tp1_challenger_10x5pct",
        }[strategy]
    else:
        position_fraction = CURRENT_SLOT_PCT
        max_total = 6
        max_standard = 5
        max_high = 1
        base_name = "current_live_5standard_1high"
    if position_fraction_override is not None:
        if position_fraction_override <= 0:
            raise ValueError("position_fraction_override must be positive")
        position_fraction = position_fraction_override
    if max_total_override is not None:
        if max_total_override <= 0:
            raise ValueError("max_total_override must be positive")
        max_total = max_total_override
    if max_exposure_fraction_override is not None and max_exposure_fraction_override <= 0:
        raise ValueError("max_exposure_fraction_override must be positive")
    strategy_name = f"entrygate_v1__{base_name}" if use_entry_gate else base_name
    if strategy_name_override:
        strategy_name = strategy_name_override

    equity = 1.0
    positions: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    closed_holds: list[float] = []
    entered = closed = missed_capacity = missed_same_symbol = filtered_entry_gate = filtered_strategy = 0
    max_open = 0
    max_exposure = 0.0
    first_entry: datetime | None = None
    last_exit: datetime | None = None

    def close_due(cutoff: datetime) -> None:
        nonlocal equity, closed, last_exit
        due = sorted(
            [p for p in positions if p["exit_at"] is not None and p["exit_at"] <= cutoff],
            key=lambda p: p["exit_at"],
        )
        for pos in due:
            gross_return = float(pos["exit_return"])
            equity += pos["notional"] * (gross_return - SHADOW_FEE_PER_FILL)
            hold = _elapsed_hours(pos["entry_at"], pos["exit_at"])
            if hold is not None:
                closed_holds.append(hold)
            last_exit = pos["exit_at"] if last_exit is None else max(last_exit, pos["exit_at"])
            positions.remove(pos)
            closed += 1

    for row in ordered:
        entry_at = row["confirmed_at"]
        close_due(entry_at)
        if use_entry_gate and not entry_gate_v1(row):
            filtered_entry_gate += 1
            continue
        episode_id = row.get("episode_id")
        if eligible_episode_ids is not None and (
            episode_id is None or int(episode_id) not in eligible_episode_ids
        ):
            filtered_strategy += 1
            continue
        symbol = str(row.get("symbol") or "")
        tier = str(row.get("risk_tier") or "standard")
        candidate_fraction = position_fraction
        if position_fraction_by_episode is not None and episode_id is not None:
            candidate_fraction = float(position_fraction_by_episode.get(int(episode_id), position_fraction))
        if candidate_fraction <= 0:
            filtered_strategy += 1
            continue
        if any(pos["symbol"] == symbol for pos in positions):
            missed_same_symbol += 1
            continue
        if len(positions) >= max_total:
            missed_capacity += 1
            continue
        if max_exposure_fraction_override is not None:
            active_fraction = sum(float(pos.get("position_fraction") or position_fraction) for pos in positions)
            if active_fraction + candidate_fraction > max_exposure_fraction_override + 1e-12:
                missed_capacity += 1
                continue
        if strategy not in {"tp5_challenger", "tp5_sl75_challenger", "hold_7d", "tp2_challenger", "tp2_10_challenger", "tp1_10_challenger"}:
            if tier == "standard" and sum(pos["tier"] == "standard" for pos in positions) >= int(max_standard or 0):
                missed_capacity += 1
                continue
            if tier == "high_risk" and sum(pos["tier"] == "high_risk" for pos in positions) >= int(max_high or 0):
                missed_capacity += 1
                continue

        target_override = None
        if target_pct_by_episode is not None and episode_id is not None:
            target_override = target_pct_by_episode.get(int(episode_id))
        exit_at, exit_return = _known_exit(
            row, strategy=strategy, generated_at=generated_at, target_pct_override=target_override
        )
        pre_fee_equity = equity
        notional = max(0.0, pre_fee_equity) * candidate_fraction
        equity -= notional * SHADOW_FEE_PER_FILL
        pos = {
            "episode_id": row.get("episode_id"),
            "symbol": symbol,
            "tier": tier,
            "entry_at": entry_at,
            "notional": notional,
            "position_fraction": candidate_fraction,
            "exit_at": exit_at,
            "exit_return": exit_return,
            "latest_return": _latest_observed_return(row),
            "mark_until": generated_at,
            "row": row,
        }
        positions.append(pos)
        accepted.append(pos)
        entered += 1
        first_entry = entry_at if first_entry is None else min(first_entry, entry_at)
        max_open = max(max_open, len(positions))
        max_exposure = max(max_exposure, sum(float(pos.get("position_fraction") or position_fraction) for pos in positions))

    close_due(generated_at)

    unmarked = 0
    marked_equity = equity
    open_holds: list[float] = []
    for pos in positions:
        hold = _elapsed_hours(pos["entry_at"], generated_at)
        if hold is not None:
            open_holds.append(hold)
        mark = pos["latest_return"]
        if mark is None:
            unmarked += 1
            continue
        marked_equity += pos["notional"] * (float(mark) - SHADOW_FEE_PER_FILL)

    all_holds = closed_holds + open_holds
    slot_days = sum(all_holds) / 24.0
    marked_return = marked_equity - 1.0
    replay_end = generated_at if positions else last_exit
    replay_span_days = None
    if first_entry is not None and replay_end is not None:
        replay_span_days = max(0.0, (replay_end - first_entry).total_seconds() / 86400.0)

    mtm = _portfolio_mtm_metrics(
        accepted,
        path_rows=path_rows,
        position_fraction=position_fraction,
    )
    max_dd = mtm["max_mtm_drawdown"]
    return PortfolioReplaySummary(
        strategy=strategy_name,
        cohort=cohort,
        signals=len(ordered),
        eligible_signals=len(ordered) - filtered_entry_gate - filtered_strategy,
        filtered_entry_gate=filtered_entry_gate,
        entered=entered,
        closed=closed,
        open_positions=len(positions),
        missed_capacity=missed_capacity,
        missed_same_symbol=missed_same_symbol,
        realized_return=equity - 1.0,
        marked_return=marked_return,
        max_open_positions=max_open,
        max_observed_exposure_pct=max_exposure,
        median_holding_hours=_median(all_holds),
        avg_holding_hours=_mean(all_holds),
        slot_days=slot_days,
        return_per_slot_day=(marked_return / slot_days) if slot_days > 0 else None,
        replay_span_days=replay_span_days,
        unmarked_open_positions=unmarked,
        mtm_points=mtm["mtm_points"],
        max_mtm_drawdown=max_dd,
        worst_mtm_return=mtm["worst_mtm_return"],
        max_unrealized_loss=mtm["max_unrealized_loss"],
        max_simultaneous_losers=mtm["max_simultaneous_losers"],
        avg_exposure_pct=mtm["avg_exposure_pct"],
        p95_exposure_pct=mtm["p95_exposure_pct"],
        avg_open_positions=mtm["avg_open_positions"],
        drawdown_recovery_hours=mtm["drawdown_recovery_hours"],
        return_over_max_drawdown=(marked_return / max_dd) if max_dd and max_dd > 0 else None,
        worst_trade_episode_id=mtm["worst_trade_episode_id"],
        worst_trade_pre_target_mae=mtm["worst_trade_pre_target_mae"],
        portfolio_return_at_worst_trade_mae=mtm["portfolio_return_at_worst_trade_mae"],
        filtered_strategy=filtered_strategy,
    )


def _volatility_bucket_label(value: float, p25: float, p50: float, p75: float) -> str:
    if value <= p25:
        return "Q1_low"
    if value <= p50:
        return "Q2"
    if value <= p75:
        return "Q3"
    return "Q4_high"


def _volatility_bucket_summary(
    rows: list[dict[str, Any]],
    *,
    cohort: str,
    risk_tier: str,
    bucket: str,
    generated_at: datetime,
) -> VolatilityBucketSummary:
    values = [value for row in rows for value in [_entry_atr_pct(row)] if value is not None]
    validation = _strategy_validation_summary(rows, strategy="tp5_sl75_challenger", generated_at=generated_at)
    tp5_validation = _strategy_validation_summary(rows, strategy="tp5_challenger", generated_at=generated_at)
    tails = {item.threshold_pct: item for item in tp5_validation.tail_ladder}
    hit_times = [
        elapsed
        for row in rows
        if row.get("target_5_at") is not None and row.get("target_5_at") <= generated_at
        for elapsed in [_elapsed_hours(row.get("confirmed_at"), row.get("target_5_at"))]
        if elapsed is not None
    ]
    return VolatilityBucketSummary(
        cohort=cohort,
        risk_tier=risk_tier,
        bucket=bucket,
        sample=len(rows),
        atr_pct_min=min(values) if values else None,
        atr_pct_max=max(values) if values else None,
        target_exits=tp5_validation.target_exits,
        stop_exits=validation.stop_exits,
        waiting=tp5_validation.waiting,
        target_rate_to_date=tp5_validation.target_rate_to_date,
        median_tp5_hours=_median(hit_times),
        avg_marked_return=validation.avg_marked_return,
        breach20_rate=tails.get(20).breach_rate if tails.get(20) else None,
        breach50_rate=tails.get(50).breach_rate if tails.get(50) else None,
        breach75_rate=tails.get(75).breach_rate if tails.get(75) else None,
        breach100_rate=tails.get(100).breach_rate if tails.get(100) else None,
    )


def _volatility_position_fractions(
    rows: list[dict[str, Any]], *, calibration_median: float | None
) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in rows:
        episode_id = row.get("episode_id")
        if episode_id is None:
            continue
        atr_pct = _entry_atr_pct(row)
        if atr_pct is None or atr_pct <= 0 or calibration_median is None or calibration_median <= 0:
            fraction = VOLATILITY_BASE_SLOT_PCT
        else:
            fraction = VOLATILITY_BASE_SLOT_PCT * calibration_median / atr_pct
            fraction = min(VOLATILITY_MAX_SLOT_PCT, max(VOLATILITY_MIN_SLOT_PCT, fraction))
        result[int(episode_id)] = fraction
    return result



def _parabolic_continuation_risk(row: dict[str, Any]) -> bool:
    snapshot = row.get("_snapshot")
    if snapshot is None:
        snapshot = json_object(row.get("feature_snapshot"))
        row["_snapshot"] = snapshot
    return parabolic_continuation_risk(snapshot)


def _parabolic_position_fractions(rows: list[dict[str, Any]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in rows:
        episode_id = row.get("episode_id")
        if episode_id is None:
            continue
        result[int(episode_id)] = (
            PARABOLIC_RISK_POSITION_PCT
            if _parabolic_continuation_risk(row)
            else PARABOLIC_RISK_BASE_POSITION_PCT
        )
    return result

def _htf_v1_state(row: dict[str, Any]) -> bool | None:
    snapshot = json_object(row.get("feature_snapshot"))
    return htf_continuation_risk(snapshot)


def _htf_v1_position_fractions(rows: list[dict[str, Any]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in rows:
        episode_id = row.get("episode_id")
        if episode_id is None:
            continue
        state = _htf_v1_state(row)
        if state is None:
            continue
        result[int(episode_id)] = HTF_FLAGGED_POSITION_FRACTION if state else HTF_BASE_POSITION_FRACTION
    return result


def _build_volatility_research(
    rows: list[dict[str, Any]],
    *,
    discovery_rows: list[dict[str, Any]],
    post_freeze_rows: list[dict[str, Any]],
    generated_at: datetime,
    path_rows: Iterable[dict[str, Any]],
    freeze_at: datetime,
) -> VolatilityResearchSummary:
    calibration_values = [
        value for row in discovery_rows for value in [_entry_atr_pct(row)] if value is not None
    ]
    p25 = _percentile(calibration_values, 0.25)
    p50 = _percentile(calibration_values, 0.50)
    p75 = _percentile(calibration_values, 0.75)

    bucket_summaries: list[VolatilityBucketSummary] = []
    if p25 is not None and p50 is not None and p75 is not None:
        for cohort_name, cohort_rows in (("all_observed", rows), ("post_freeze", post_freeze_rows)):
            assigned: dict[str, list[dict[str, Any]]] = {name: [] for name in ("Q1_low", "Q2", "Q3", "Q4_high")}
            for row in cohort_rows:
                value = _entry_atr_pct(row)
                if value is None:
                    continue
                assigned[_volatility_bucket_label(value, p25, p50, p75)].append(row)
            for tier in ("all", "standard", "high_risk"):
                for bucket, group in assigned.items():
                    selected = group if tier == "all" else [
                        row for row in group if str(row.get("risk_tier") or "standard") == tier
                    ]
                    if selected:
                        bucket_summaries.append(_volatility_bucket_summary(
                            selected, cohort=cohort_name, risk_tier=tier, bucket=bucket, generated_at=generated_at
                        ))

    fractions = _volatility_position_fractions(rows, calibration_median=p50)
    fixed = _portfolio_replay(
        rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="volatility_comparison_all_observed", strategy_name_override="tp5_sl75_fixed_6x5_30pct",
    )
    normalized = _portfolio_replay(
        rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="volatility_comparison_all_observed", strategy_name_override="tp5_sl75_atr_normalized_6slot_30pct",
        max_total_override=VOLATILITY_MAX_SLOTS, position_fraction_by_episode=fractions,
        max_exposure_fraction_override=VOLATILITY_MAX_EXPOSURE_PCT,
    )
    post_fractions = _volatility_position_fractions(post_freeze_rows, calibration_median=p50)
    prospective_fixed = _portfolio_replay(
        post_freeze_rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="volatility_comparison_post_freeze", strategy_name_override="post_freeze_tp5_sl75_fixed_6x5_30pct",
    )
    prospective_normalized = _portfolio_replay(
        post_freeze_rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="volatility_comparison_post_freeze", strategy_name_override="post_freeze_tp5_sl75_atr_normalized_6slot_30pct",
        max_total_override=VOLATILITY_MAX_SLOTS, position_fraction_by_episode=post_fractions,
        max_exposure_fraction_override=VOLATILITY_MAX_EXPOSURE_PCT,
    )

    parabolic_rows = [row for row in rows if _parabolic_continuation_risk(row)]
    non_parabolic_rows = [row for row in rows if not _parabolic_continuation_risk(row)]
    post_parabolic_rows = [row for row in post_freeze_rows if _parabolic_continuation_risk(row)]
    parabolic_fractions = _parabolic_position_fractions(rows)
    post_parabolic_fractions = _parabolic_position_fractions(post_freeze_rows)
    parabolic_de_risked = _portfolio_replay(
        rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="parabolic_risk_all_observed",
        strategy_name_override="tp5_sl75_parabolic_2_5pct_else_5pct_6slot_30pct",
        max_total_override=PARABOLIC_RISK_MAX_SLOTS,
        position_fraction_by_episode=parabolic_fractions,
        max_exposure_fraction_override=PARABOLIC_RISK_MAX_EXPOSURE_PCT,
    )
    prospective_parabolic_de_risked = _portfolio_replay(
        post_freeze_rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="parabolic_risk_post_freeze",
        strategy_name_override="post_freeze_tp5_sl75_parabolic_2_5pct_else_5pct_6slot_30pct",
        max_total_override=PARABOLIC_RISK_MAX_SLOTS,
        position_fraction_by_episode=post_parabolic_fractions,
        max_exposure_fraction_override=PARABOLIC_RISK_MAX_EXPOSURE_PCT,
    )
    htf_flagged_rows = [row for row in rows if _htf_v1_state(row) is True]
    htf_unflagged_rows = [row for row in rows if _htf_v1_state(row) is False]
    htf_computable_ids = {
        int(row["episode_id"]) for row in rows
        if row.get("episode_id") is not None and _htf_v1_state(row) is not None
    }
    htf_fractions = _htf_v1_position_fractions(rows)
    htf_de_risked = _portfolio_replay(
        rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="htf_v1_all_observed",
        strategy_name_override="tp5_sl75_htf_v1_2_5pct_else_5pct_6slot_30pct",
        max_total_override=6,
        position_fraction_by_episode=htf_fractions,
        max_exposure_fraction_override=0.30,
        eligible_episode_ids=htf_computable_ids,
    )

    observed_valid = sum(_entry_atr_pct(row) is not None for row in rows)
    return VolatilityResearchSummary(
        freeze_at=freeze_at,
        calibration_sample=len(calibration_values),
        observed_sample=observed_valid,
        missing_atr=len(rows) - observed_valid,
        calibration_p25=p25,
        calibration_median=p50,
        calibration_p75=p75,
        size_floor=VOLATILITY_MIN_SLOT_PCT,
        size_base=VOLATILITY_BASE_SLOT_PCT,
        size_ceiling=VOLATILITY_MAX_SLOT_PCT,
        max_slots=VOLATILITY_MAX_SLOTS,
        max_exposure=VOLATILITY_MAX_EXPOSURE_PCT,
        buckets=tuple(bucket_summaries),
        portfolio_fixed=fixed,
        portfolio_normalized=normalized,
        prospective_portfolio_fixed=prospective_fixed,
        prospective_portfolio_normalized=prospective_normalized,
        parabolic_return_24h_threshold=PARABOLIC_RISK_RETURN_24H,
        parabolic_ema_distance_atr_threshold=PARABOLIC_RISK_EMA_DISTANCE_ATR,
        parabolic_position_fraction=PARABOLIC_RISK_POSITION_PCT,
        parabolic_flagged_validation=_strategy_validation_summary(
            parabolic_rows, strategy="tp5_sl75_challenger", generated_at=generated_at
        ),
        parabolic_unflagged_validation=_strategy_validation_summary(
            non_parabolic_rows, strategy="tp5_sl75_challenger", generated_at=generated_at
        ),
        parabolic_portfolio_de_risked=parabolic_de_risked,
        prospective_parabolic_flagged_validation=_strategy_validation_summary(
            post_parabolic_rows, strategy="tp5_sl75_challenger", generated_at=generated_at
        ),
        prospective_parabolic_portfolio_de_risked=prospective_parabolic_de_risked,
        htf_computable_signals=len(htf_computable_ids),
        htf_missing_signals=len(rows) - len(htf_computable_ids),
        htf_flagged_validation=_strategy_validation_summary(
            htf_flagged_rows, strategy="tp5_sl75_challenger", generated_at=generated_at
        ),
        htf_unflagged_validation=_strategy_validation_summary(
            htf_unflagged_rows, strategy="tp5_sl75_challenger", generated_at=generated_at
        ),
        htf_portfolio_de_risked=htf_de_risked,
    )


def _calendar_throughput_comparison(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
    path_rows: Iterable[dict[str, Any]],
) -> CalendarThroughputComparison:
    observed = sorted(
        [
            row for row in rows
            if row.get("confirmed_at") is not None and row["confirmed_at"] <= generated_at
        ],
        key=lambda row: row["confirmed_at"],
    )
    history_start = observed[0]["confirmed_at"] if observed else None
    history_span_days = (
        max(0.0, (generated_at - history_start).total_seconds() / 86400.0)
        if history_start is not None else 0.0
    )
    current = _portfolio_replay(
        observed, strategy="current", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    tp5 = _portfolio_replay(
        observed, strategy="tp5_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    tp5_sl75 = _portfolio_replay(
        observed, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    hold_7d = _portfolio_replay(
        observed, strategy="hold_7d", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    tp2 = _portfolio_replay(
        observed, strategy="tp2_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    tp2_10 = _portfolio_replay(
        observed, strategy="tp2_10_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )
    tp1_10 = _portfolio_replay(
        observed, strategy="tp1_10_challenger", generated_at=generated_at, path_rows=path_rows,
        cohort="calendar_observed_all_signals",
    )

    latest_start: datetime | None = None
    latest_current: PortfolioReplaySummary | None = None
    latest_tp5: PortfolioReplaySummary | None = None
    latest_tp5_sl75: PortfolioReplaySummary | None = None
    latest_hold_7d: PortfolioReplaySummary | None = None
    latest_tp2: PortfolioReplaySummary | None = None
    latest_tp2_10: PortfolioReplaySummary | None = None
    latest_tp1_10: PortfolioReplaySummary | None = None
    if history_start is not None and history_span_days >= CALENDAR_MONTH_DAYS:
        latest_start = generated_at - timedelta(days=CALENDAR_MONTH_DAYS)
        latest_rows = [
            row for row in observed
            if latest_start <= row["confirmed_at"] <= generated_at
        ]
        # Each monthly window is an equal-footing $1-equity / empty-book replay at
        # the exact window start. This avoids extrapolating a short backtest into a
        # monthly return and makes Current vs TP5 consume the same arriving signals.
        latest_current = _portfolio_replay(
            latest_rows, strategy="current", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_tp5 = _portfolio_replay(
            latest_rows, strategy="tp5_challenger", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_tp5_sl75 = _portfolio_replay(
            latest_rows, strategy="tp5_sl75_challenger", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_hold_7d = _portfolio_replay(
            latest_rows, strategy="hold_7d", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_tp2 = _portfolio_replay(
            latest_rows, strategy="tp2_challenger", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_tp2_10 = _portfolio_replay(
            latest_rows, strategy="tp2_10_challenger", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )
        latest_tp1_10 = _portfolio_replay(
            latest_rows, strategy="tp1_10_challenger", generated_at=generated_at, path_rows=path_rows,
            cohort="calendar_latest_30d_empty_book",
        )

    return CalendarThroughputComparison(
        history_start=history_start,
        history_end=generated_at,
        history_span_days=history_span_days,
        current=current,
        tp5=tp5,
        tp5_sl75=tp5_sl75,
        hold_7d=hold_7d,
        tp2=tp2,
        tp2_10=tp2_10,
        tp1_10=tp1_10,
        latest_30d_current=latest_current,
        latest_30d_tp5=latest_tp5,
        latest_30d_tp5_sl75=latest_tp5_sl75,
        latest_30d_hold_7d=latest_hold_7d,
        latest_30d_tp2=latest_tp2,
        latest_30d_tp2_10=latest_tp2_10,
        latest_30d_tp1_10=latest_tp1_10,
        latest_30d_start=latest_start,
        days_until_30d=max(0.0, CALENDAR_MONTH_DAYS - history_span_days),
    )


def persistent_run_risk_flags(row: dict[str, Any]) -> tuple[bool | None, bool | None]:
    """Return the frozen research-only long-run and strict continuation-risk flags.

    Both inputs are available at signal time. Missing inputs remain unknown rather
    than being silently treated as safe. The strict flag means a run lasted at
    least 36h while price was no more than 3 ATR above the 4h EMA20 at signal time.
    """
    run_hours = _float(row.get("hours_run_to_breakdown"))
    if run_hours is None:
        return None, None
    long_flag = run_hours >= PERSISTENT_RUN_LONG_HOURS
    if not long_flag:
        return False, False
    ema_spec = FEATURE_BY_KEY["distance_above_ema20_atr_4h"]
    ema_distance = _float(_feature_value(row, ema_spec))
    strict_flag = None if ema_distance is None else ema_distance <= PERSISTENT_RUN_MAX_EMA_DISTANCE_ATR
    return True, strict_flag


def _adverse_100_within(
    row: dict[str, Any], hours: int, *, as_of: datetime | None = None
) -> bool:
    confirmed = row.get("confirmed_at")
    breached = row.get("adverse_100_at")
    return bool(
        isinstance(confirmed, datetime)
        and isinstance(breached, datetime)
        and breached <= confirmed + timedelta(hours=hours)
        and (as_of is None or breached <= as_of)
    )


def _tp5_before_adverse_100(
    row: dict[str, Any], hours: int, *, as_of: datetime | None = None
) -> bool:
    if not _adverse_100_within(row, hours, as_of=as_of):
        return False
    target = row.get("target_5_at")
    breached = row.get("adverse_100_at")
    return bool(isinstance(target, datetime) and isinstance(breached, datetime) and target < breached)


def _persistent_run_risk_research(
    rows: list[dict[str, Any]], *, generated_at: datetime
) -> PersistentRunRiskResearchSummary:
    observation = PERSISTENT_RUN_RISK_OBSERVATION_HOURS
    # Freeze the retrospective evidence exactly as it was knowable when the
    # candidate was selected. Early -100% events from younger signals count as
    # resolved; non-breaches only count after a full 120h observation.
    calibration = [
        row for row in rows
        if isinstance(row.get("confirmed_at"), datetime)
        and row["confirmed_at"] <= PERSISTENT_RUN_RISK_FREEZE_AT
    ]
    prospective = [
        row for row in rows
        if isinstance(row.get("confirmed_at"), datetime)
        and PERSISTENT_RUN_RISK_FREEZE_AT < row["confirmed_at"] <= generated_at
    ]

    result: list[PersistentRunRiskBucketSummary] = []
    for cohort_name, cohort_rows, cohort_as_of in (
        ("calibration", calibration, PERSISTENT_RUN_RISK_FREEZE_AT),
        ("prospective", prospective, generated_at),
    ):
        for flag_name, flag_index in (("long_run_36h", 0), ("persistent_run_36h_ema3", 1)):
            assignments: list[tuple[dict[str, Any], bool]] = []
            for row in cohort_rows:
                flags = persistent_run_risk_flags(row)
                value = flags[flag_index]
                if value is not None:
                    assignments.append((row, value))
            for flagged in (True, False):
                group = [row for row, value in assignments if value is flagged]
                # A breach is a resolved event even when the rest of the 120h path
                # is incomplete. A non-breach only becomes evaluable once 120h of
                # path is complete, preventing right-censoring bias.
                evaluable = [
                    row for row in group
                    if _adverse_100_within(row, observation, as_of=cohort_as_of)
                    or (
                        isinstance(row.get("confirmed_at"), datetime)
                        and row["confirmed_at"] + timedelta(hours=observation) <= cohort_as_of
                        and _path_complete_for_hours(row, observation)
                    )
                ]
                breaches = [
                    row for row in evaluable
                    if _adverse_100_within(row, observation, as_of=cohort_as_of)
                ]
                result.append(
                    PersistentRunRiskBucketSummary(
                        cohort=cohort_name,
                        flag_name=flag_name,
                        flagged=flagged,
                        signals=len(group),
                        evaluable_120h=len(evaluable),
                        adverse_100_breaches=len(breaches),
                        adverse_100_rate=(len(breaches) / len(evaluable)) if evaluable else None,
                        tp5_before_adverse_100=sum(
                            _tp5_before_adverse_100(row, observation, as_of=cohort_as_of)
                            for row in breaches
                        ),
                    )
                )
    return PersistentRunRiskResearchSummary(
        freeze_at=PERSISTENT_RUN_RISK_FREEZE_AT,
        nonbreach_maturity_cutoff=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF,
        observation_hours=observation,
        long_run_hours=PERSISTENT_RUN_LONG_HOURS,
        max_ema_distance_atr=PERSISTENT_RUN_MAX_EMA_DISTANCE_ATR,
        buckets=tuple(result),
    )


def _prospective_cohort_summary(
    rows: list[dict[str, Any]], *, cohort: str, freeze_at: datetime
) -> ProspectiveCohortSummary:
    complete = [row for row in rows if row.get("return_168h_pct") is not None and _path_complete_7d(row)]
    # This cohort card intentionally asks the fixed-horizon research question:
    # "did TP5 occur within seven days?". It does NOT define the live TP5 exit.
    risk = _tp5_risk_summary(complete, max_hours=168)
    gate_eligible = sum(entry_gate_v1(row) for row in rows)
    gate_complete = sum(entry_gate_v1(row) for row in complete)
    return ProspectiveCohortSummary(
        cohort=cohort,
        signal_cutoff=freeze_at,
        signals=len(rows),
        complete_7d=len(complete),
        tp5_hits=risk.hits,
        tp5_hit_rate=risk.hit_rate,
        median_tp5_hours=risk.median_time_hours,
        worst_pre_tp5_adverse=risk.worst_adverse_before_target,
        entrygate_eligible=gate_eligible,
        entrygate_eligible_rate=(gate_eligible / len(rows)) if rows else None,
        entrygate_complete_7d=gate_complete,
    )


def _prospective_tp5_live_summary(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
    path_rows: Iterable[dict[str, Any]],
) -> ProspectiveTp5LiveSummary:
    hit_rows: list[dict[str, Any]] = []
    waiting_rows: list[dict[str, Any]] = []
    for row in rows:
        target = row.get("target_5_at")
        if target is not None and target <= generated_at:
            hit_rows.append(row)
        else:
            waiting_rows.append(row)

    hit_times = [
        value
        for row in hit_rows
        for value in [_elapsed_hours(row.get("confirmed_at"), row.get("target_5_at"))]
        if value is not None
    ]
    pre_hit_adverse: list[float] = []
    for row in hit_rows:
        if "path_mae_before_target_5" not in row:
            continue
        raw = _float(row.get("path_mae_before_target_5"))
        pre_hit_adverse.append(max(0.0, -raw) if raw is not None else 0.0)

    waiting_ids = {int(row["episode_id"]) for row in waiting_rows if row.get("episode_id") is not None}
    waiting_min_close: dict[int, float] = {}
    for path in path_rows:
        episode_id = path.get("episode_id")
        if episode_id is None or int(episode_id) not in waiting_ids:
            continue
        value = _float(path.get("close_return_pct"))
        if value is None:
            continue
        episode_id = int(episode_id)
        waiting_min_close[episode_id] = min(value, waiting_min_close.get(episode_id, value))
    waiting_adverse = [max(0.0, -value) for value in waiting_min_close.values()]
    waiting_ages = [
        value
        for row in waiting_rows
        for value in [_elapsed_hours(row.get("confirmed_at"), generated_at)]
        if value is not None
    ]
    return ProspectiveTp5LiveSummary(
        signals=len(rows),
        hits=len(hit_rows),
        waiting=len(waiting_rows),
        waiting_over_7d=sum(value >= 168.0 for value in waiting_ages),
        observed_hit_rate=(len(hit_rows) / len(rows)) if rows else None,
        median_hit_hours=_median(hit_times),
        p75_hit_hours=_percentile(hit_times, 0.75),
        worst_pre_hit_adverse=max(pre_hit_adverse) if pre_hit_adverse else None,
        worst_waiting_close_adverse=max(waiting_adverse) if waiting_adverse else None,
        oldest_waiting_hours=max(waiting_ages) if waiting_ages else None,
    )


def _prospective_gate_acceptance_summary(
    rows: list[dict[str, Any]],
    *,
    rolling_window: int = PROSPECTIVE_GATE_ROLLING_WINDOW,
) -> ProspectiveGateAcceptanceSummary:
    ordered = sorted(
        [row for row in rows if row.get("confirmed_at") is not None],
        key=lambda row: row["confirmed_at"],
    )
    eligible = sum(entry_gate_v1(row) for row in ordered)
    recent = ordered[-rolling_window:]
    recent_eligible = sum(entry_gate_v1(row) for row in recent)
    return ProspectiveGateAcceptanceSummary(
        signals=len(ordered),
        eligible=eligible,
        eligible_rate=(eligible / len(ordered)) if ordered else None,
        rolling_window=rolling_window,
        rolling_signals=len(recent),
        rolling_eligible=recent_eligible,
        rolling_eligible_rate=(recent_eligible / len(recent)) if recent else None,
    )


def _regime_metric_value(row: dict[str, Any], feature: str) -> float | None:
    if feature == "entry_quality_score":
        return float(shadow_entry_scores(row)[0])
    if feature == "continuation_risk_score":
        return float(shadow_entry_scores(row)[1])
    spec = FEATURE_BY_KEY.get(feature)
    if spec is None:
        return None
    return _float(_feature_value(row, spec))


def _prospective_regime_drift(
    discovery_rows: list[dict[str, Any]],
    post_freeze_rows: list[dict[str, Any]],
) -> tuple[RegimeDriftSummary, ...]:
    features = (
        ("exhaustion_score", "Exhaustion"),
        ("run_score", "Run"),
        ("amount_24h", "24h turnover"),
        ("return_24h", "24h pump"),
        ("volume_zscore_15m", "15m volume z"),
        ("entry_quality_score", "Entry Quality"),
        ("continuation_risk_score", "Continuation Risk"),
    )
    result: list[RegimeDriftSummary] = []
    for feature, label in features:
        discovery = [
            value for row in discovery_rows
            for value in [_regime_metric_value(row, feature)] if value is not None
        ]
        post = [
            value for row in post_freeze_rows
            for value in [_regime_metric_value(row, feature)] if value is not None
        ]
        p25 = _percentile(discovery, 0.25)
        p75 = _percentile(discovery, 0.75)
        if post and p25 is not None and p75 is not None:
            below = sum(value < p25 for value in post) / len(post)
            above = sum(value > p75 for value in post) / len(post)
            inside = 1.0 - below - above
        else:
            below = inside = above = None
        result.append(RegimeDriftSummary(
            feature=feature,
            feature_label=label,
            discovery_sample=len(discovery),
            post_freeze_sample=len(post),
            discovery_median=_median(discovery),
            post_freeze_median=_median(post),
            discovery_p25=p25,
            discovery_p75=p75,
            post_below_discovery_p25_rate=below,
            post_inside_discovery_iqr_rate=inside,
            post_above_discovery_p75_rate=above,
        ))
    return tuple(result)


def _cohort_score_buckets(rows: list[dict[str, Any]], cohort: str) -> tuple[CohortScoreBucketSummary, ...]:
    complete = [row for row in rows if row.get("return_168h_pct") is not None and _path_complete_7d(row)]
    return tuple(
        CohortScoreBucketSummary(
            cohort=cohort,
            score_name=item.score_name,
            bucket=item.bucket,
            sample=item.sample,
            target_20_rate_7d=item.target_20_rate_7d,
            positive_7d_rate=item.positive_7d_rate,
            avg_return_7d=item.avg_return_7d,
        )
        for item in _build_score_buckets(complete)
    )


def build_research_analytics(
    raw_rows: Iterable[dict[str, Any]],
    *,
    generated_at: datetime,
    delayed_entry_rows: Iterable[dict[str, Any]] = (),
    portfolio_path_rows: Iterable[dict[str, Any]] = (),
    regime_history_rows: Iterable[dict[str, Any]] = (),
    oos_freeze_at: datetime = RESEARCH_OOS_FREEZE_AT,
) -> ResearchAnalyticsReport:
    rows = [dict(row) for row in raw_rows]
    rows = [row for row in rows if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    standard_rows = [row for row in rows if str(row.get("risk_tier") or "standard") == "standard"]
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
    discovery_rows = [row for row in rows if row.get("confirmed_at") is not None and row["confirmed_at"] <= oos_freeze_at]
    post_freeze_rows = [row for row in rows if row.get("confirmed_at") is not None and row["confirmed_at"] > oos_freeze_at]
    post_freeze_standard_rows = [row for row in post_freeze_rows if str(row.get("risk_tier") or "standard") == "standard"]
    prospective_cohorts = (
        _prospective_cohort_summary(discovery_rows, cohort="discovery", freeze_at=oos_freeze_at),
        _prospective_cohort_summary(post_freeze_rows, cohort="post_freeze", freeze_at=oos_freeze_at),
    )
    prospective_score_buckets = (
        *_cohort_score_buckets(discovery_rows, "discovery"),
        *_cohort_score_buckets(post_freeze_rows, "post_freeze"),
    )
    portfolio_path_rows = tuple(dict(row) for row in portfolio_path_rows)
    post_freeze_complete_paths_7d = [
        row for row in complete_paths_7d
        if row.get("confirmed_at") is not None and row["confirmed_at"] > oos_freeze_at
    ]
    prospective_tp5_live = _prospective_tp5_live_summary(
        post_freeze_rows, generated_at=generated_at, path_rows=portfolio_path_rows
    )
    prospective_strategy_validations = tuple(
        _strategy_validation_summary(post_freeze_rows, strategy=strategy, generated_at=generated_at)
        for strategy in ("tp5_challenger", "tp5_sl75_challenger", "hold_7d")
    )
    prospective_strategy_portfolios = tuple(
        _portfolio_replay(
            post_freeze_rows, strategy=strategy, generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="post_freeze_observed_all_signals",
        )
        for strategy in ("tp5_challenger", "tp5_sl75_challenger", "hold_7d")
    )
    prospective_gate_acceptance = _prospective_gate_acceptance_summary(post_freeze_rows)
    prospective_regime_drift = _prospective_regime_drift(discovery_rows, post_freeze_rows)
    prospective_standard_tp5_validation = replace(
        _strategy_validation_summary(post_freeze_standard_rows, strategy="tp5_challenger", generated_at=generated_at),
        strategy="standard_tp5_10x5",
        label="STANDARD TP5 • 10×5%",
        rule="STANDARD only • +5% target • no stop • no timeout • 10×5% / 50% cap",
    )
    prospective_standard_tp5_sl75_validation = replace(
        _strategy_validation_summary(post_freeze_standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at),
        strategy="standard_tp5_sl75_10x5",
        label="STANDARD TP5 + SL75 • 10×5%",
        rule="STANDARD only • +5% target • -75% catastrophic stop • no timeout • 10×5% / 50% cap",
    )
    prospective_portfolio_standard_tp5_10 = _portfolio_replay(
        post_freeze_standard_rows, strategy="tp5_challenger", generated_at=generated_at,
        path_rows=portfolio_path_rows, cohort="post_freeze_standard_only",
        strategy_name_override="standard_tp5_10x5_50pct",
        position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT,
        max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
    )
    prospective_portfolio_standard_tp5_10x75 = _portfolio_replay(
        post_freeze_standard_rows, strategy="tp5_challenger", generated_at=generated_at,
        path_rows=portfolio_path_rows, cohort="post_freeze_standard_only",
        strategy_name_override="standard_tp5_10x7_5_75pct",
        position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_75,
        max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
    )
    prospective_portfolio_standard_tp5_10x10 = _portfolio_replay(
        post_freeze_standard_rows, strategy="tp5_challenger", generated_at=generated_at,
        path_rows=portfolio_path_rows, cohort="post_freeze_standard_only",
        strategy_name_override="standard_tp5_10x10_100pct",
        position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_100,
        max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
    )
    prospective_portfolio_standard_tp5_sl75_10 = _portfolio_replay(
        post_freeze_standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at,
        path_rows=portfolio_path_rows, cohort="post_freeze_standard_only",
        strategy_name_override="standard_tp5_sl75_10x5_50pct",
        position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT,
        max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
    )
    prospective_portfolio_standard_tp5_sl75_10x10 = _portfolio_replay(
        post_freeze_standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at,
        path_rows=portfolio_path_rows, cohort="post_freeze_standard_only",
        strategy_name_override="standard_tp5_sl75_10x10_100pct",
        position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_100,
        max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
    )
    calendar_throughput = _calendar_throughput_comparison(
        rows, generated_at=generated_at, path_rows=portfolio_path_rows
    )
    prospective_portfolios = (
        _portfolio_replay(
            post_freeze_complete_paths_7d, strategy="current", generated_at=generated_at,
            path_rows=portfolio_path_rows,
        ),
        _portfolio_replay(
            post_freeze_complete_paths_7d, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows,
        ),
        _portfolio_replay(
            post_freeze_complete_paths_7d, strategy="current", generated_at=generated_at,
            path_rows=portfolio_path_rows, use_entry_gate=True,
        ),
        _portfolio_replay(
            post_freeze_complete_paths_7d, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, use_entry_gate=True,
        ),
    )
    token_regime = build_token_regime_research(
        rows, regime_history_rows, discovery_cutoff=oos_freeze_at
    )
    profile_by_episode = {item.episode_id: item for item in token_regime.profiles}
    all_episode_ids = {int(row["episode_id"]) for row in rows if row.get("episode_id") is not None}
    follower_ids = {
        item.episode_id for item in token_regime.profiles
        if item.behavior_class == REGIME_FOLLOWER_CLASS
    }
    episodic_ids = {
        item.episode_id for item in token_regime.profiles
        if item.behavior_class == EPISODIC_CLASS
    }
    no_regime_ids = all_episode_ids - follower_ids
    priority_scores = {
        item.episode_id: float(item.episodic_score)
        for item in token_regime.profiles if item.episodic_score is not None
    }
    regime_portfolios = (
        _portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="calendar_observed_all_signals",
            eligible_episode_ids=no_regime_ids,
            strategy_name_override="tp5_no_regime_followers",
        ),
        _portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="calendar_observed_all_signals",
            eligible_episode_ids=episodic_ids,
            strategy_name_override="tp5_episodic_only",
        ),
        _portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="calendar_observed_all_signals",
            priority_scores=priority_scores,
            strategy_name_override="tp5_episodic_priority_same_bar",
        ),
    )
    # Hybrid exits keep the frozen TP5-6 sizing/capacity rules and vary only the
    # full-profit target by the pre-signal token behaviour class. Unclassified /
    # insufficient-history episodes deliberately default to TP5 rather than being
    # silently assigned the faster exit.
    hybrid_1_targets = {
        item.episode_id: (2 if item.behavior_class == REGIME_FOLLOWER_CLASS else 5)
        for item in token_regime.profiles
    }
    hybrid_2_targets = {
        item.episode_id: (2 if item.behavior_class in {MIXED_CLASS, REGIME_FOLLOWER_CLASS} else 5)
        for item in token_regime.profiles
    }
    persistent_run_risk = _persistent_run_risk_research(rows, generated_at=generated_at)
    hybrid_portfolios = (
        _portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="calendar_observed_all_signals",
            target_pct_by_episode=hybrid_1_targets,
            strategy_name_override="hybrid1_ep_mix_tp5_regime_tp2",
        ),
        _portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="calendar_observed_all_signals",
            target_pct_by_episode=hybrid_2_targets,
            strategy_name_override="hybrid2_ep_tp5_mix_regime_tp2",
        ),
    )
    volatility = _build_volatility_research(
        rows, discovery_rows=discovery_rows, post_freeze_rows=post_freeze_rows,
        generated_at=generated_at, path_rows=portfolio_path_rows, freeze_at=oos_freeze_at,
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
        # Live TP5 validation is not a seven-day strategy. Use every observed
        # signal and keep unresolved positions open indefinitely until target.
        tp5_risk=_tp5_risk_summary(rows, generated_at=generated_at),
        strategy_validations=tuple(
            _strategy_validation_summary(rows, strategy=strategy, generated_at=generated_at)
            for strategy in ("tp5_challenger", "tp5_sl75_challenger", "hold_7d")
        ),
        portfolio_current=_portfolio_replay(
            complete_paths_7d, strategy="current", generated_at=generated_at,
            path_rows=portfolio_path_rows,
        ),
        portfolio_tp5=_portfolio_replay(
            rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_all_signals_open_until_exit",
        ),
        portfolio_tp5_sl75=_portfolio_replay(
            rows, strategy="tp5_sl75_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_all_signals_open_until_exit",
        ),
        portfolio_hold_7d=_portfolio_replay(
            rows, strategy="hold_7d", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_all_signals_open_until_exit",
        ),
        standard_tp5_validation=replace(
            _strategy_validation_summary(standard_rows, strategy="tp5_challenger", generated_at=generated_at),
            strategy="standard_tp5_10x5",
            label="STANDARD TP5 • 10×5%",
            rule="STANDARD only • +5% target • no stop • no timeout • 10×5% / 50% cap",
        ),
        standard_tp5_sl75_validation=replace(
            _strategy_validation_summary(standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at),
            strategy="standard_tp5_sl75_10x5",
            label="STANDARD TP5 + SL75 • 10×5%",
            rule="STANDARD only • +5% target • -75% catastrophic stop • no timeout • 10×5% / 50% cap",
        ),
        portfolio_standard_tp5_10=_portfolio_replay(
            standard_rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_standard_only",
            strategy_name_override="standard_tp5_10x5_50pct",
            position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT,
            max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
        ),
        portfolio_standard_tp5_10x75=_portfolio_replay(
            standard_rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_standard_only",
            strategy_name_override="standard_tp5_10x7_5_75pct",
            position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_75,
            max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
        ),
        portfolio_standard_tp5_10x10=_portfolio_replay(
            standard_rows, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_standard_only",
            strategy_name_override="standard_tp5_10x10_100pct",
            position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_100,
            max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
        ),
        portfolio_standard_tp5_sl75_10=_portfolio_replay(
            standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_standard_only",
            strategy_name_override="standard_tp5_sl75_10x5_50pct",
            position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT,
            max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
        ),
        portfolio_standard_tp5_sl75_10x10=_portfolio_replay(
            standard_rows, strategy="tp5_sl75_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, cohort="observed_standard_only",
            strategy_name_override="standard_tp5_sl75_10x10_100pct",
            position_fraction_override=STANDARD_TP5_SCALE_SLOT_PCT_100,
            max_total_override=STANDARD_TP5_SCALE_MAX_SLOTS,
        ),
        portfolio_entrygate_current=_portfolio_replay(
            complete_paths_7d, strategy="current", generated_at=generated_at,
            path_rows=portfolio_path_rows, use_entry_gate=True,
        ),
        portfolio_entrygate_tp5=_portfolio_replay(
            complete_paths_7d, strategy="tp5_challenger", generated_at=generated_at,
            path_rows=portfolio_path_rows, use_entry_gate=True,
        ),
        prospective_cohorts=prospective_cohorts,
        prospective_score_buckets=prospective_score_buckets,
        prospective_tp5_live=prospective_tp5_live,
        prospective_strategy_validations=prospective_strategy_validations,
        prospective_strategy_portfolios=prospective_strategy_portfolios,
        prospective_standard_tp5_validation=prospective_standard_tp5_validation,
        prospective_standard_tp5_sl75_validation=prospective_standard_tp5_sl75_validation,
        prospective_portfolio_standard_tp5_10=prospective_portfolio_standard_tp5_10,
        prospective_portfolio_standard_tp5_10x75=prospective_portfolio_standard_tp5_10x75,
        prospective_portfolio_standard_tp5_10x10=prospective_portfolio_standard_tp5_10x10,
        prospective_portfolio_standard_tp5_sl75_10=prospective_portfolio_standard_tp5_sl75_10,
        prospective_portfolio_standard_tp5_sl75_10x10=prospective_portfolio_standard_tp5_sl75_10x10,
        prospective_gate_acceptance=prospective_gate_acceptance,
        prospective_regime_drift=prospective_regime_drift,
        prospective_portfolios=prospective_portfolios,
        calendar_throughput=calendar_throughput,
        token_regime=token_regime,
        regime_portfolios=regime_portfolios,
        hybrid_portfolios=hybrid_portfolios,
        persistent_run_risk=persistent_run_risk,
        volatility=volatility,
        oos_freeze_at=oos_freeze_at,
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
        "analysis", "risk_tier_or_strategy", "horizon_or_threshold", "sample",
        "metric_1", "metric_2", "metric_3", "metric_4", "metric_5", "metric_6",
        "metric_7", "metric_8", "metric_9", "metric_10", "metric_11", "metric_12",
    ])
    for item in report.standard_exit_sweep:
        writer.writerow([
            "standard_exit_horizon_paired", item.risk_tier, f"{item.horizon_hours}h", item.sample,
            _csv_pct(item.avg_return), _csv_pct(item.median_return), _csv_pct(item.positive_rate),
            _csv_pct(item.avg_return_per_day), f"cohort_complete_{item.cohort_horizon_hours}h", "",
            "", "", "", "", "", "",
        ])
    for item in report.high_risk_timeout_sweep:
        writer.writerow([
            "high_risk_tp20_timeout_paired_10d", "high_risk", f"{item.timeout_hours}h", item.sample,
            _csv_pct(item.avg_strategy_return), _csv_pct(item.median_strategy_return),
            _csv_pct(item.target_hit_rate), _csv_pct(item.worst_strategy_return),
            "" if item.avg_holding_hours is None else f"{item.avg_holding_hours:.6f}",
            _csv_pct(item.return_per_slot_day), item.wins, item.losses,
            _csv_pct(item.sum_strategy_return), _csv_pct(item.best_strategy_return), "", "",
        ])
    for item in report.stop_survival:
        writer.writerow([
            "winner_stop_survival", item.risk_tier, f"{item.stop_pct}%", item.winners_with_path,
            item.winners_killed, _csv_pct(item.kill_rate), _csv_pct(item.survivor_rate), "", "", "",
            "", "", "", "", "", "",
        ])

    risk = report.tp5_risk
    writer.writerow([
        "tp5_pre_hit_summary_observed", "all_public_tiers", "open_until_tp5", risk.sample,
        risk.hits, _csv_pct(risk.hit_rate),
        "" if risk.median_time_hours is None else f"{risk.median_time_hours:.6f}",
        "" if risk.p75_time_hours is None else f"{risk.p75_time_hours:.6f}",
        _csv_pct(risk.median_adverse_before_target),
        _csv_pct(risk.p75_adverse_before_target),
        _csv_pct(risk.worst_adverse_before_target), "", "", "", "", "",
    ])
    for item in risk.adverse_races:
        writer.writerow([
            "tp5_adverse_race_observed", "all_public_tiers", f"-{item.adverse_threshold_pct}%", item.sample,
            item.target_first, _csv_pct(item.target_first_rate), item.adverse_first,
            item.same_candle, item.unresolved, "", "", "", "", "", "", "",
        ])

    for summary in report.strategy_validations:
        writer.writerow([
            "strategy_validation_observed", summary.strategy, summary.rule, summary.sample,
            summary.resolved, summary.target_exits, summary.stop_exits, summary.timeout_exits,
            summary.waiting, _csv_pct(summary.resolved_positive_rate),
            _csv_pct(summary.avg_exit_return), _csv_pct(summary.median_exit_return),
            _csv_pct(summary.worst_exit_return),
            "" if summary.median_holding_hours is None else f"{summary.median_holding_hours:.6f}",
            "" if summary.p75_holding_hours is None else f"{summary.p75_holding_hours:.6f}", "",
        ])
        for tail in summary.tail_ladder:
            writer.writerow([
                "strategy_tail_observed", summary.strategy, f"-{tail.threshold_pct}%", summary.sample,
                tail.breached_before_exit_or_mark, _csv_pct(tail.breach_rate),
                tail.later_tp5_after_breach, "", "", "", "", "", "", "", "", "",
            ])

    for portfolio in (
        report.portfolio_current, report.portfolio_tp5, report.portfolio_tp5_sl75, report.portfolio_hold_7d,
        report.portfolio_entrygate_current, report.portfolio_entrygate_tp5,
    ):
        writer.writerow([
            ("portfolio_replay_observed" if portfolio.cohort == "observed_all_signals_open_until_exit" else "portfolio_replay_paired_7d"), portfolio.strategy, portfolio.cohort, portfolio.signals,
            portfolio.eligible_signals, portfolio.entered, portfolio.closed,
            portfolio.missed_capacity, portfolio.missed_same_symbol,
            _csv_pct(portfolio.realized_return), _csv_pct(portfolio.marked_return),
            _csv_pct(portfolio.max_mtm_drawdown), _csv_pct(portfolio.worst_mtm_return),
            _csv_pct(portfolio.avg_exposure_pct), _csv_pct(portfolio.p95_exposure_pct),
            _csv_pct(portfolio.return_per_slot_day),
        ])
        writer.writerow([
            "portfolio_mtm_detail", portfolio.strategy, "15m_close_marked", portfolio.mtm_points,
            _csv_pct(portfolio.max_mtm_drawdown), _csv_pct(portfolio.worst_mtm_return),
            _csv_pct(portfolio.max_unrealized_loss), portfolio.max_simultaneous_losers,
            _csv_pct(portfolio.avg_exposure_pct), _csv_pct(portfolio.p95_exposure_pct),
            "" if portfolio.avg_open_positions is None else f"{portfolio.avg_open_positions:.6f}",
            "" if portfolio.drawdown_recovery_hours is None else f"{portfolio.drawdown_recovery_hours:.6f}",
            "" if portfolio.return_over_max_drawdown is None else f"{portfolio.return_over_max_drawdown:.6f}",
            portfolio.worst_trade_episode_id or "", _csv_pct(portfolio.worst_trade_pre_target_mae),
            _csv_pct(portfolio.portfolio_return_at_worst_trade_mae),
        ])
    for cohort in report.prospective_cohorts:
        writer.writerow([
            "prospective_oos_cohort", cohort.cohort, cohort.signal_cutoff.isoformat(), cohort.signals,
            cohort.complete_7d, cohort.tp5_hits, _csv_pct(cohort.tp5_hit_rate),
            "" if cohort.median_tp5_hours is None else f"{cohort.median_tp5_hours:.6f}",
            _csv_pct(cohort.worst_pre_tp5_adverse), cohort.entrygate_eligible,
            _csv_pct(cohort.entrygate_eligible_rate), cohort.entrygate_complete_7d, "", "", "", "",
        ])
    live = report.prospective_tp5_live
    writer.writerow([
        "prospective_tp5_live", "post_freeze", "open_until_tp5", live.signals,
        live.hits, live.waiting, live.waiting_over_7d, _csv_pct(live.observed_hit_rate), "",
        "" if live.median_hit_hours is None else f"{live.median_hit_hours:.6f}",
        "" if live.p75_hit_hours is None else f"{live.p75_hit_hours:.6f}",
        _csv_pct(live.worst_pre_hit_adverse), _csv_pct(live.worst_waiting_close_adverse),
        "" if live.oldest_waiting_hours is None else f"{live.oldest_waiting_hours:.6f}", "", "",
    ])
    gate = report.prospective_gate_acceptance
    writer.writerow([
        "prospective_entrygate_acceptance", "entrygate_v1", f"rolling_{gate.rolling_window}", gate.signals,
        gate.eligible, _csv_pct(gate.eligible_rate), gate.rolling_signals, gate.rolling_eligible,
        _csv_pct(gate.rolling_eligible_rate), "", "", "", "", "", "", "",
    ])
    for portfolio in report.prospective_portfolios:
        writer.writerow([
            "prospective_portfolio_replay_paired_7d", portfolio.strategy, "post_freeze", portfolio.signals,
            portfolio.eligible_signals, portfolio.entered, portfolio.closed,
            portfolio.missed_capacity, portfolio.missed_same_symbol,
            _csv_pct(portfolio.realized_return), _csv_pct(portfolio.marked_return),
            _csv_pct(portfolio.max_mtm_drawdown),
            "" if portfolio.return_over_max_drawdown is None else f"{portfolio.return_over_max_drawdown:.6f}",
            _csv_pct(portfolio.return_per_slot_day), _csv_pct(portfolio.avg_exposure_pct),
            _csv_pct(portfolio.p95_exposure_pct),
        ])

    calendar = report.calendar_throughput
    for portfolio in report.hybrid_portfolios:
        writer.writerow([
            "token_regime_hybrid_portfolio", portfolio.strategy, portfolio.cohort, portfolio.signals,
            portfolio.entered, portfolio.closed, portfolio.open_positions, portfolio.missed_capacity,
            portfolio.missed_same_symbol, _csv_pct(portfolio.marked_return),
            _csv_pct(portfolio.max_mtm_drawdown), _csv_pct(portfolio.avg_exposure_pct),
            _csv_pct(portfolio.p95_exposure_pct),
            "" if portfolio.slot_days is None else f"{portfolio.slot_days:.6f}",
            _csv_pct(portfolio.return_per_slot_day),
            "" if not calendar.history_span_days else f"{portfolio.closed / calendar.history_span_days:.6f}",
        ])
    for portfolio in (calendar.current, calendar.tp5, calendar.tp5_sl75, calendar.hold_7d, calendar.tp2, calendar.tp2_10, calendar.tp1_10):
        writer.writerow([
            "calendar_throughput_observed", portfolio.strategy, portfolio.cohort, portfolio.signals,
            portfolio.entered, portfolio.closed, portfolio.open_positions, portfolio.missed_capacity,
            portfolio.missed_same_symbol, _csv_pct(portfolio.marked_return),
            _csv_pct(portfolio.max_mtm_drawdown), _csv_pct(portfolio.avg_exposure_pct),
            _csv_pct(portfolio.p95_exposure_pct),
            f"{calendar.history_span_days:.6f}",
            "" if not calendar.history_span_days else f"{portfolio.entered / calendar.history_span_days:.6f}",
            "" if not portfolio.signals else f"{portfolio.entered / portfolio.signals:.6f}",
        ])
    if all(item is not None for item in (calendar.latest_30d_current, calendar.latest_30d_tp5, calendar.latest_30d_tp5_sl75, calendar.latest_30d_hold_7d, calendar.latest_30d_tp2, calendar.latest_30d_tp2_10, calendar.latest_30d_tp1_10)):
        for portfolio in (calendar.latest_30d_current, calendar.latest_30d_tp5, calendar.latest_30d_tp5_sl75, calendar.latest_30d_hold_7d, calendar.latest_30d_tp2, calendar.latest_30d_tp2_10, calendar.latest_30d_tp1_10):
            writer.writerow([
                "calendar_latest_30d_empty_book", portfolio.strategy, portfolio.cohort, portfolio.signals,
                portfolio.entered, portfolio.closed, portfolio.open_positions, portfolio.missed_capacity,
                portfolio.missed_same_symbol, _csv_pct(portfolio.marked_return),
                _csv_pct(portfolio.max_mtm_drawdown), _csv_pct(portfolio.avg_exposure_pct),
                _csv_pct(portfolio.p95_exposure_pct),
                f"{CALENDAR_MONTH_DAYS}",
                f"{portfolio.entered / CALENDAR_MONTH_DAYS:.6f}",
                "" if not portfolio.signals else f"{portfolio.entered / portfolio.signals:.6f}",
            ])
    return output.getvalue().encode("utf-8")


def research_token_regime_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "episode_id", "symbol", "confirmed_at", "behavior_class",
        "paired_4h_returns", "history_days", "btc_correlation", "btc_beta",
        "market_r2", "isolated_pump_count", "isolated_pump_rate_pct",
        "positive_spike_concentration_pct", "episodic_score",
    ])
    for item in report.token_regime.profiles:
        writer.writerow([
            item.episode_id, item.symbol, item.confirmed_at.isoformat(), item.behavior_class,
            item.paired_returns, f"{item.history_days:.6f}",
            "" if item.btc_correlation is None else f"{item.btc_correlation:.8f}",
            "" if item.btc_beta is None else f"{item.btc_beta:.8f}",
            "" if item.market_r2 is None else f"{item.market_r2:.8f}",
            item.isolated_pump_count,
            _csv_pct(item.isolated_pump_rate),
            _csv_pct(item.positive_spike_concentration),
            "" if item.episodic_score is None else f"{item.episodic_score:.8f}",
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
    for item in report.prospective_score_buckets:
        writer.writerow([
            "prospective_score_bucket", f"{item.cohort}:{item.score_name}", item.bucket, item.sample,
            _csv_pct(item.target_20_rate_7d), _csv_pct(item.positive_7d_rate),
            _csv_pct(item.avg_return_7d), "", "",
        ])
    for item in report.prospective_regime_drift:
        extra = (
            f"discovery_median={item.discovery_median};post_median={item.post_freeze_median};"
            f"discovery_p25={item.discovery_p25};discovery_p75={item.discovery_p75};"
            f"post_below_p25={item.post_below_discovery_p25_rate};"
            f"post_inside_iqr={item.post_inside_discovery_iqr_rate};"
            f"post_above_p75={item.post_above_discovery_p75_rate}"
        )
        writer.writerow([
            "prospective_regime_drift", item.feature_label, item.feature, item.post_freeze_sample,
            "", "", "", "", extra,
        ])
    return output.getvalue().encode("utf-8")


def research_strategy_validation_csv(report: ResearchAnalyticsReport) -> bytes:
    """Compact machine-readable comparison of core strategies plus research challengers."""
    portfolios = {
        "tp5_challenger": report.portfolio_tp5,
        "tp5_sl75_challenger": report.portfolio_tp5_sl75,
        "hold_7d": report.portfolio_hold_7d,
        "standard_tp5_10x5": report.portfolio_standard_tp5_10,
        "standard_tp5_10x7_5": report.portfolio_standard_tp5_10x75,
        "standard_tp5_10x10": report.portfolio_standard_tp5_10x10,
        "standard_tp5_sl75_10x5": report.portfolio_standard_tp5_sl75_10,
        "standard_tp5_sl75_10x10": report.portfolio_standard_tp5_sl75_10x10,
    }
    output = io.StringIO(newline="")
    fields = [
        "strategy", "label", "rule", "signal_sample", "resolved", "target_exits",
        "stop_exits", "timeout_exits", "waiting", "positive_exits", "negative_exits",
        "resolved_positive_rate_pct", "target_rate_to_date_pct", "avg_exit_return_pct",
        "median_exit_return_pct", "best_exit_return_pct", "worst_exit_return_pct",
        "marked_sample", "marked_positive_rate_pct", "avg_marked_return_pct",
        "median_marked_return_pct", "sum_marked_return_pct",
        "median_holding_hours", "p75_holding_hours",
        "breach20_count", "breach20_rate_pct", "breach20_later_tp5",
        "breach50_count", "breach50_rate_pct", "breach50_later_tp5",
        "breach75_count", "breach75_rate_pct", "breach75_later_tp5",
        "breach100_count", "breach100_rate_pct", "breach100_later_tp5",
        "portfolio_entered", "portfolio_closed", "portfolio_open", "capacity_misses",
        "same_symbol_misses", "capture_rate_pct", "realized_account_return_pct", "marked_account_return_pct",
        "thirty_day_equivalent_return_pct", "max_mtm_drawdown_pct", "return_over_drawdown", "avg_exposure_pct",
        "p95_exposure_pct", "slot_days", "return_per_slot_day_pct",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    standard_5 = report.standard_tp5_validation
    standard_75 = replace(
        report.standard_tp5_validation,
        strategy="standard_tp5_10x7_5",
        label="STANDARD TP5 • 10×7.5%",
        rule="STANDARD only • +5% target • no stop • no timeout • 10×7.5% / 75% cap",
    )
    standard_10 = replace(
        report.standard_tp5_validation,
        strategy="standard_tp5_10x10",
        label="STANDARD TP5 • 10×10%",
        rule="STANDARD only • +5% target • no stop • no timeout • 10×10% / 100% cap",
    )
    standard_sl75_5 = report.standard_tp5_sl75_validation
    standard_sl75_10 = replace(
        report.standard_tp5_sl75_validation,
        strategy="standard_tp5_sl75_10x10",
        label="STANDARD TP5 + SL75 • 10×10%",
        rule="STANDARD only • +5% target • -75% catastrophic stop • no timeout • 10×10% / 100% cap",
    )
    summaries = (
        *report.strategy_validations,
        standard_5,
        standard_sl75_5,
        standard_75,
        standard_10,
        standard_sl75_10,
    )
    for summary in summaries:
        portfolio = portfolios[summary.strategy]
        tails = {item.threshold_pct: item for item in summary.tail_ladder}
        row: dict[str, Any] = {
            "strategy": summary.strategy,
            "label": summary.label,
            "rule": summary.rule,
            "signal_sample": summary.sample,
            "resolved": summary.resolved,
            "target_exits": summary.target_exits,
            "stop_exits": summary.stop_exits,
            "timeout_exits": summary.timeout_exits,
            "waiting": summary.waiting,
            "positive_exits": summary.positive_exits,
            "negative_exits": summary.negative_exits,
            "resolved_positive_rate_pct": _csv_pct(summary.resolved_positive_rate),
            "target_rate_to_date_pct": _csv_pct(summary.target_rate_to_date),
            "avg_exit_return_pct": _csv_pct(summary.avg_exit_return),
            "median_exit_return_pct": _csv_pct(summary.median_exit_return),
            "best_exit_return_pct": _csv_pct(summary.best_exit_return),
            "worst_exit_return_pct": _csv_pct(summary.worst_exit_return),
            "marked_sample": summary.marked_sample,
            "marked_positive_rate_pct": _csv_pct(summary.marked_positive_rate),
            "avg_marked_return_pct": _csv_pct(summary.avg_marked_return),
            "median_marked_return_pct": _csv_pct(summary.median_marked_return),
            "sum_marked_return_pct": _csv_pct(summary.sum_marked_return),
            "median_holding_hours": "" if summary.median_holding_hours is None else f"{summary.median_holding_hours:.6f}",
            "p75_holding_hours": "" if summary.p75_holding_hours is None else f"{summary.p75_holding_hours:.6f}",
            "portfolio_entered": portfolio.entered,
            "portfolio_closed": portfolio.closed,
            "portfolio_open": portfolio.open_positions,
            "capacity_misses": portfolio.missed_capacity,
            "same_symbol_misses": portfolio.missed_same_symbol,
            "capture_rate_pct": _csv_pct((portfolio.entered / portfolio.eligible_signals) if portfolio.eligible_signals else None),
            "realized_account_return_pct": _csv_pct(portfolio.realized_return),
            "marked_account_return_pct": _csv_pct(portfolio.marked_return),
            "thirty_day_equivalent_return_pct": _csv_pct((portfolio.marked_return * 30.0 / portfolio.replay_span_days) if portfolio.replay_span_days else None),
            "max_mtm_drawdown_pct": _csv_pct(portfolio.max_mtm_drawdown),
            "return_over_drawdown": "" if portfolio.return_over_max_drawdown is None else f"{portfolio.return_over_max_drawdown:.6f}",
            "avg_exposure_pct": _csv_pct(portfolio.avg_exposure_pct),
            "p95_exposure_pct": _csv_pct(portfolio.p95_exposure_pct),
            "slot_days": f"{portfolio.slot_days:.6f}",
            "return_per_slot_day_pct": _csv_pct(portfolio.return_per_slot_day),
        }
        for threshold in STRATEGY_TAIL_THRESHOLDS_PCT:
            tail = tails.get(threshold)
            row[f"breach{threshold}_count"] = tail.breached_before_exit_or_mark if tail else 0
            row[f"breach{threshold}_rate_pct"] = _csv_pct(tail.breach_rate) if tail else ""
            row[f"breach{threshold}_later_tp5"] = tail.later_tp5_after_breach if tail else 0
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def research_volatility_csv(report: ResearchAnalyticsReport) -> bytes:
    output = io.StringIO(newline="")
    fields = [
        "row_type", "cohort", "risk_tier", "bucket", "sample", "atr_pct_min", "atr_pct_max",
        "tp5", "sl75", "waiting", "tp5_rate_to_date_pct", "median_tp5_hours",
        "avg_marked_return_pct", "breach20_rate_pct", "breach50_rate_pct", "breach75_rate_pct",
        "breach100_rate_pct", "strategy", "entered", "capture_rate_pct", "marked_account_return_pct",
        "max_mtm_drawdown_pct", "return_over_drawdown", "avg_exposure_pct", "p95_exposure_pct",
        "calibration_p25_atr_pct", "calibration_median_atr_pct", "calibration_p75_atr_pct",
        "size_floor_pct", "size_base_pct", "size_ceiling_pct", "max_slots", "max_exposure_pct",
        "parabolic_return_24h_threshold_pct", "parabolic_ema_distance_atr_threshold",
        "parabolic_position_pct",
        "htf_computable_signals", "htf_missing_signals",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    v = report.volatility
    for item in v.buckets:
        writer.writerow({
            "row_type": "volatility_bucket", "cohort": item.cohort, "risk_tier": item.risk_tier,
            "bucket": item.bucket, "sample": item.sample,
            "atr_pct_min": _csv_pct(item.atr_pct_min), "atr_pct_max": _csv_pct(item.atr_pct_max),
            "tp5": item.target_exits, "sl75": item.stop_exits, "waiting": item.waiting,
            "tp5_rate_to_date_pct": _csv_pct(item.target_rate_to_date),
            "median_tp5_hours": "" if item.median_tp5_hours is None else f"{item.median_tp5_hours:.6f}",
            "avg_marked_return_pct": _csv_pct(item.avg_marked_return),
            "breach20_rate_pct": _csv_pct(item.breach20_rate), "breach50_rate_pct": _csv_pct(item.breach50_rate),
            "breach75_rate_pct": _csv_pct(item.breach75_rate), "breach100_rate_pct": _csv_pct(item.breach100_rate),
            "calibration_p25_atr_pct": _csv_pct(v.calibration_p25),
            "calibration_median_atr_pct": _csv_pct(v.calibration_median),
            "calibration_p75_atr_pct": _csv_pct(v.calibration_p75),
            "size_floor_pct": _csv_pct(v.size_floor), "size_base_pct": _csv_pct(v.size_base),
            "size_ceiling_pct": _csv_pct(v.size_ceiling), "max_slots": v.max_slots,
            "max_exposure_pct": _csv_pct(v.max_exposure),
            "parabolic_return_24h_threshold_pct": _csv_pct(v.parabolic_return_24h_threshold),
            "parabolic_ema_distance_atr_threshold": f"{v.parabolic_ema_distance_atr_threshold:.6f}",
            "parabolic_position_pct": _csv_pct(v.parabolic_position_fraction),
        })
    for cohort, label, summary in (
        ("all_observed", "flagged", v.parabolic_flagged_validation),
        ("all_observed", "unflagged", v.parabolic_unflagged_validation),
        ("post_freeze", "flagged", v.prospective_parabolic_flagged_validation),
    ):
        writer.writerow({
            "row_type": "parabolic_risk_bucket", "cohort": cohort, "bucket": label,
            "sample": summary.sample, "tp5": summary.target_exits, "sl75": summary.stop_exits,
            "waiting": summary.waiting, "tp5_rate_to_date_pct": _csv_pct(summary.target_rate_to_date),
            "avg_marked_return_pct": _csv_pct(summary.avg_marked_return),
            "parabolic_return_24h_threshold_pct": _csv_pct(v.parabolic_return_24h_threshold),
            "parabolic_ema_distance_atr_threshold": f"{v.parabolic_ema_distance_atr_threshold:.6f}",
            "parabolic_position_pct": _csv_pct(v.parabolic_position_fraction),
            "max_slots": v.max_slots, "max_exposure_pct": _csv_pct(v.max_exposure),
        })
    for label, summary in (("flagged", v.htf_flagged_validation), ("unflagged", v.htf_unflagged_validation)):
        writer.writerow({
            "row_type": "htf_v1_bucket", "cohort": "all_observed", "bucket": label,
            "sample": summary.sample, "tp5": summary.target_exits, "sl75": summary.stop_exits,
            "waiting": summary.waiting, "tp5_rate_to_date_pct": _csv_pct(summary.target_rate_to_date),
            "avg_marked_return_pct": _csv_pct(summary.avg_marked_return),
            "max_slots": 6, "max_exposure_pct": _csv_pct(0.30),
            "htf_computable_signals": v.htf_computable_signals,
            "htf_missing_signals": v.htf_missing_signals,
        })
    for cohort, portfolio in (
        ("all_observed", v.portfolio_fixed), ("all_observed", v.portfolio_normalized),
        ("all_observed", v.parabolic_portfolio_de_risked),
        ("all_observed", v.htf_portfolio_de_risked),
        ("post_freeze", v.prospective_portfolio_fixed), ("post_freeze", v.prospective_portfolio_normalized),
        ("post_freeze", v.prospective_parabolic_portfolio_de_risked),
    ):
        writer.writerow({
            "row_type": "portfolio", "cohort": cohort, "strategy": portfolio.strategy,
            "sample": portfolio.eligible_signals, "entered": portfolio.entered,
            "capture_rate_pct": _csv_pct((portfolio.entered / portfolio.eligible_signals) if portfolio.eligible_signals else None),
            "marked_account_return_pct": _csv_pct(portfolio.marked_return),
            "max_mtm_drawdown_pct": _csv_pct(portfolio.max_mtm_drawdown),
            "return_over_drawdown": "" if portfolio.return_over_max_drawdown is None else f"{portfolio.return_over_max_drawdown:.6f}",
            "avg_exposure_pct": _csv_pct(portfolio.avg_exposure_pct), "p95_exposure_pct": _csv_pct(portfolio.p95_exposure_pct),
            "calibration_p25_atr_pct": _csv_pct(v.calibration_p25),
            "calibration_median_atr_pct": _csv_pct(v.calibration_median),
            "calibration_p75_atr_pct": _csv_pct(v.calibration_p75),
            "size_floor_pct": _csv_pct(v.size_floor), "size_base_pct": _csv_pct(v.size_base),
            "size_ceiling_pct": _csv_pct(v.size_ceiling), "max_slots": v.max_slots,
            "max_exposure_pct": _csv_pct(v.max_exposure),
            "parabolic_return_24h_threshold_pct": _csv_pct(v.parabolic_return_24h_threshold),
            "parabolic_ema_distance_atr_threshold": f"{v.parabolic_ema_distance_atr_threshold:.6f}",
            "parabolic_position_pct": _csv_pct(v.parabolic_position_fraction),
            "htf_computable_signals": v.htf_computable_signals,
            "htf_missing_signals": v.htf_missing_signals,
        })
    return output.getvalue().encode("utf-8")


def research_signal_dataset_csv(rows: Iterable[dict[str, Any]], *, generated_at: datetime | None = None) -> bytes:
    normalized = [dict(row) for row in rows]
    normalized = [row for row in normalized if str(row.get("risk_tier") or "standard") in PUBLIC_RESEARCH_RISK_TIERS]
    fixed = [
        "episode_id", "symbol", "risk_tier", "confirmed_at", "entry_price",
        "return_24h_pct", "return_48h_pct", "return_72h_pct", "return_168h_pct",
        "target_20_at", "target_20_path_at", "path_rows", "path_last_at", "path_latest_return",
        "path_rows_7d", "path_rows_14d",
        "path_mfe_7d", "path_mae_7d", "path_mae_before_target_5", "path_mae_before_target_5_at",
        "path_mae_before_target_20", "path_mfe_at", "path_mae_at",
        "path_mfe_14d", "path_mae_14d", "path_mfe_14d_at", "path_mae_14d_at",
        "atr_15m",
    ]
    horizon_fields = [f"path_return_{hours}h" for hours in STANDARD_EXIT_HORIZONS_HOURS]
    target_fields = [f"target_{pct}_at" for pct in TARGET_LEVELS_PCT if pct != 20]
    adverse_race_fields = [f"adverse_{pct}_at" for pct in TP5_ADVERSE_THRESHOLDS_PCT]
    feature_fields = [spec.key for spec in FEATURE_SPECS]
    score_fields = [
        "shadow_entry_quality_score", "shadow_continuation_risk_score",
        "entry_gate_v1_eligible", "prospective_cohort",
        "persistent_run_long_flag", "persistent_run_strict_flag",
        "persistent_run_risk_cohort",
        "htf_v1_version", "htf_v1_computable", "htf_v1_flagged",
        "htf_v1_missing_fields", "htf_v1_position_fraction",
    ]
    strategy_fields: list[str] = []
    for prefix in ("tp5_indefinite", "tp5_sl75", "hold_7d"):
        strategy_fields.extend([
            f"{prefix}_status", f"{prefix}_exit_at", f"{prefix}_exit_return_pct",
            f"{prefix}_holding_hours",
        ])
    writer_fields = fixed + horizon_fields + target_fields + adverse_race_fields + feature_fields + score_fields + strategy_fields

    if generated_at is None:
        observed_times = [
            value for row in normalized
            for value in (row.get("path_last_at"), row.get("confirmed_at"))
            if isinstance(value, datetime)
        ]
        generated_at = max(observed_times) if observed_times else datetime.now(UTC)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=writer_fields, extrasaction="ignore")
    writer.writeheader()
    for row in normalized:
        snapshot = json_object(row.get("feature_snapshot"))
        flattened: dict[str, Any] = {}
        for key in fixed + horizon_fields + target_fields + adverse_race_fields:
            value = snapshot.get("atr_15m") if key == "atr_15m" else row.get(key)
            if isinstance(value, datetime):
                value = value.isoformat()
            flattened[key] = value
        for spec in FEATURE_SPECS:
            if spec.source == "row":
                flattened[spec.key] = row.get(spec.key)
            elif spec.source == "derived":
                flattened[spec.key] = _feature_value(row, spec)
            else:
                flattened[spec.key] = snapshot.get(spec.key)
        quality, continuation = shadow_entry_scores(row)
        flattened["shadow_entry_quality_score"] = quality
        flattened["shadow_continuation_risk_score"] = continuation
        flattened["entry_gate_v1_eligible"] = entry_gate_v1(row)
        long_flag, strict_flag = persistent_run_risk_flags(row)
        flattened["persistent_run_long_flag"] = long_flag
        flattened["persistent_run_strict_flag"] = strict_flag
        confirmed = row.get("confirmed_at")
        flattened["prospective_cohort"] = (
            "post_freeze"
            if isinstance(confirmed, datetime) and confirmed > RESEARCH_OOS_FREEZE_AT
            else "discovery"
        )
        if isinstance(confirmed, datetime) and confirmed > PERSISTENT_RUN_RISK_FREEZE_AT:
            flattened["persistent_run_risk_cohort"] = "prospective"
        else:
            flattened["persistent_run_risk_cohort"] = "calibration"
        htf_state = htf_continuation_risk(snapshot)
        flattened["htf_v1_version"] = snapshot.get("htf_v1_version") or "htf_unresolved_bull_v1"
        flattened["htf_v1_computable"] = htf_state is not None
        flattened["htf_v1_flagged"] = htf_state
        required_htf = ("return_24h", "cross_section_percentile", "distance_above_ema20_atr_4h", "previous_momentum_1h")
        flattened["htf_v1_missing_fields"] = ";".join(
            key for key in required_htf if snapshot.get(key) is None
        )
        flattened["htf_v1_position_fraction"] = (
            HTF_FLAGGED_POSITION_FRACTION if htf_state is True
            else HTF_BASE_POSITION_FRACTION if htf_state is False
            else ""
        )
        for strategy, prefix in (
            ("tp5_challenger", "tp5_indefinite"),
            ("tp5_sl75_challenger", "tp5_sl75"),
            ("hold_7d", "hold_7d"),
        ):
            status, exit_at, exit_return = _strategy_outcome(
                row, strategy=strategy, generated_at=generated_at
            )
            flattened[f"{prefix}_status"] = status
            flattened[f"{prefix}_exit_at"] = exit_at.isoformat() if isinstance(exit_at, datetime) else ""
            flattened[f"{prefix}_exit_return_pct"] = _csv_pct(exit_return)
            hold = _elapsed_hours(confirmed, exit_at) if isinstance(confirmed, datetime) else None
            flattened[f"{prefix}_holding_hours"] = "" if hold is None else f"{hold:.6f}"
        writer.writerow(flattened)
    return output.getvalue().encode("utf-8")
