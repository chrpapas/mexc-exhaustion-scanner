from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable

# Frozen v1 research methodology. These constants are intentionally not environment
# variables: changing them after seeing outcomes would turn the shadow comparison into
# threshold tuning rather than a prospective hypothesis test.
REGIME_LOOKBACK_DAYS = 90
REGIME_MIN_PAIRED_RETURNS = 180  # ~30 days of completed 4h returns
ISOLATED_PUMP_MIN_RETURN = 0.05
ISOLATED_PUMP_MIN_BTC_OUTPERFORMANCE = 0.04
SPIKE_TOP_BARS = 5
REGIME_FOLLOWER_QUANTILE = 1.0 / 3.0
EPISODIC_QUANTILE = 2.0 / 3.0
INSUFFICIENT_CLASS = "INSUFFICIENT"
REGIME_FOLLOWER_CLASS = "REGIME_FOLLOWER"
MIXED_CLASS = "MIXED"
EPISODIC_CLASS = "EPISODIC"


@dataclass(frozen=True, slots=True)
class TokenRegimeProfile:
    episode_id: int
    symbol: str
    confirmed_at: datetime
    paired_returns: int
    history_days: float
    btc_correlation: float | None
    btc_beta: float | None
    market_r2: float | None
    isolated_pump_count: int
    isolated_pump_rate: float | None
    positive_spike_concentration: float | None
    episodic_score: float | None = None
    behavior_class: str = INSUFFICIENT_CLASS


@dataclass(frozen=True, slots=True)
class TokenBehaviorBucketSummary:
    behavior_class: str
    signals: int
    tp5_hits: int
    tp5_hit_rate: float | None
    median_tp5_hours: float | None
    p75_tp5_hours: float | None
    median_pre_tp5_adverse: float | None
    p75_pre_tp5_adverse: float | None


@dataclass(frozen=True, slots=True)
class TokenRegimeResearchSummary:
    lookback_days: int
    minimum_paired_returns: int
    profiled_signals: int
    insufficient_signals: int
    discovery_profiled_signals: int
    follower_score_max: float | None
    episodic_score_min: float | None
    buckets: tuple[TokenBehaviorBucketSummary, ...]
    profiles: tuple[TokenRegimeProfile, ...]


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom <= 0:
        return None
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(dx, dy, strict=True)) / denom))


def _beta(token_returns: list[float], btc_returns: list[float]) -> float | None:
    if len(token_returns) != len(btc_returns) or len(token_returns) < 2:
        return None
    mt = statistics.fmean(token_returns)
    mb = statistics.fmean(btc_returns)
    var_btc = sum((b - mb) ** 2 for b in btc_returns)
    if var_btc <= 0:
        return None
    cov = sum(
        (t - mt) * (b - mb)
        for t, b in zip(token_returns, btc_returns, strict=True)
    )
    return cov / var_btc


def _empirical_percentile(reference: list[float], value: float) -> float:
    ordered = sorted(reference)
    if not ordered:
        return 0.5
    # Mid-rank empirical CDF keeps ties from being pushed to an extreme bucket.
    left = bisect.bisect_left(ordered, value)
    right = bisect.bisect_right(ordered, value)
    return ((left + right) / 2.0) / len(ordered)


def raw_regime_profiles(history_rows: Iterable[dict[str, Any]]) -> tuple[TokenRegimeProfile, ...]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for raw in history_rows:
        episode_id = raw.get("episode_id")
        if episode_id is None:
            continue
        grouped.setdefault(int(episode_id), []).append(dict(raw))

    result: list[TokenRegimeProfile] = []
    for episode_id, rows in grouped.items():
        rows.sort(key=lambda row: row.get("open_time"))
        first = rows[0]
        symbol = str(first.get("symbol") or "")
        confirmed_at = first.get("confirmed_at")
        if not isinstance(confirmed_at, datetime):
            continue

        token_returns: list[float] = []
        btc_returns: list[float] = []
        times: list[datetime] = []
        previous_token: float | None = None
        previous_btc: float | None = None
        for row in rows:
            token_close = _float(row.get("token_close"))
            btc_close = _float(row.get("btc_close"))
            open_time = row.get("open_time")
            if token_close is None or btc_close is None or token_close <= 0 or btc_close <= 0:
                previous_token = token_close
                previous_btc = btc_close
                continue
            if previous_token is not None and previous_btc is not None and previous_token > 0 and previous_btc > 0:
                token_returns.append(token_close / previous_token - 1.0)
                btc_returns.append(btc_close / previous_btc - 1.0)
                if isinstance(open_time, datetime):
                    times.append(open_time)
            previous_token = token_close
            previous_btc = btc_close

        paired = len(token_returns)
        history_days = 0.0
        if len(times) >= 2:
            history_days = max(0.0, (times[-1] - times[0]).total_seconds() / 86400.0)

        corr = _pearson(token_returns, btc_returns) if paired >= 2 else None
        beta = _beta(token_returns, btc_returns) if paired >= 2 else None
        # Negative correlation is not "regime following" for a long-only market beta
        # interpretation, so explanatory power is based on positive correlation only.
        market_r2 = max(0.0, corr or 0.0) ** 2 if corr is not None else None
        isolated_count = sum(
            1
            for token_ret, btc_ret in zip(token_returns, btc_returns, strict=True)
            if token_ret >= ISOLATED_PUMP_MIN_RETURN
            and token_ret - btc_ret >= ISOLATED_PUMP_MIN_BTC_OUTPERFORMANCE
        )
        isolated_rate = isolated_count / paired if paired else None
        positive = sorted((ret for ret in token_returns if ret > 0), reverse=True)
        positive_total = sum(positive)
        spike_concentration = (
            sum(positive[:SPIKE_TOP_BARS]) / positive_total if positive_total > 0 else None
        )

        result.append(
            TokenRegimeProfile(
                episode_id=episode_id,
                symbol=symbol,
                confirmed_at=confirmed_at,
                paired_returns=paired,
                history_days=history_days,
                btc_correlation=corr,
                btc_beta=beta,
                market_r2=market_r2,
                isolated_pump_count=isolated_count,
                isolated_pump_rate=isolated_rate,
                positive_spike_concentration=spike_concentration,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.confirmed_at, item.episode_id)))


def classify_regime_profiles(
    profiles: Iterable[TokenRegimeProfile],
    *,
    discovery_cutoff: datetime,
) -> tuple[tuple[TokenRegimeProfile, ...], float | None, float | None, int]:
    raw = list(profiles)
    discovery = [
        item
        for item in raw
        if item.confirmed_at < discovery_cutoff
        and item.paired_returns >= REGIME_MIN_PAIRED_RETURNS
        and item.market_r2 is not None
        and item.isolated_pump_rate is not None
        and item.positive_spike_concentration is not None
    ]
    r2_ref = [float(item.market_r2) for item in discovery]
    isolated_ref = [float(item.isolated_pump_rate) for item in discovery]
    spike_ref = [float(item.positive_spike_concentration) for item in discovery]

    scored: list[TokenRegimeProfile] = []
    for item in raw:
        if (
            item.paired_returns < REGIME_MIN_PAIRED_RETURNS
            or item.market_r2 is None
            or item.isolated_pump_rate is None
            or item.positive_spike_concentration is None
            or not discovery
        ):
            scored.append(item)
            continue
        independence = 1.0 - _empirical_percentile(r2_ref, float(item.market_r2))
        isolated = _empirical_percentile(isolated_ref, float(item.isolated_pump_rate))
        spikiness = _empirical_percentile(spike_ref, float(item.positive_spike_concentration))
        score = statistics.fmean((independence, isolated, spikiness))
        scored.append(replace(item, episodic_score=score))

    discovery_scores = [
        float(item.episodic_score)
        for item in scored
        if item.confirmed_at < discovery_cutoff and item.episodic_score is not None
    ]
    follower_max = _percentile(discovery_scores, REGIME_FOLLOWER_QUANTILE)
    episodic_min = _percentile(discovery_scores, EPISODIC_QUANTILE)

    classified: list[TokenRegimeProfile] = []
    for item in scored:
        if item.episodic_score is None or follower_max is None or episodic_min is None:
            behavior = INSUFFICIENT_CLASS
        elif item.episodic_score <= follower_max:
            behavior = REGIME_FOLLOWER_CLASS
        elif item.episodic_score >= episodic_min:
            behavior = EPISODIC_CLASS
        else:
            behavior = MIXED_CLASS
        classified.append(replace(item, behavior_class=behavior))
    return tuple(classified), follower_max, episodic_min, len(discovery_scores)


def _elapsed_hours(start: Any, end: Any) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0.0, (end - start).total_seconds() / 3600.0)


def build_token_regime_research(
    signal_rows: Iterable[dict[str, Any]],
    history_rows: Iterable[dict[str, Any]],
    *,
    discovery_cutoff: datetime,
) -> TokenRegimeResearchSummary:
    signals = {int(row["episode_id"]): dict(row) for row in signal_rows if row.get("episode_id") is not None}
    raw = raw_regime_profiles(history_rows)
    profiles, follower_max, episodic_min, discovery_count = classify_regime_profiles(
        raw, discovery_cutoff=discovery_cutoff
    )

    profile_by_episode = {item.episode_id: item for item in profiles}
    buckets: list[TokenBehaviorBucketSummary] = []
    order = (EPISODIC_CLASS, MIXED_CLASS, REGIME_FOLLOWER_CLASS, INSUFFICIENT_CLASS)
    for behavior in order:
        member_ids = {
            item.episode_id
            for item in profiles
            if item.behavior_class == behavior and item.episode_id in signals
        }
        if behavior == INSUFFICIENT_CLASS:
            member_ids.update(set(signals) - set(profile_by_episode))
        hit_times: list[float] = []
        adverse: list[float] = []
        hits = 0
        for episode_id in sorted(member_ids):
            row = signals[episode_id]
            target_at = row.get("target_5_at")
            hours = _elapsed_hours(row.get("confirmed_at"), target_at)
            if hours is not None:
                hits += 1
                hit_times.append(hours)
            mae = _float(row.get("path_mae_before_target_5"))
            if mae is not None:
                adverse.append(max(0.0, -mae))
        buckets.append(
            TokenBehaviorBucketSummary(
                behavior_class=behavior,
                signals=len(member_ids),
                tp5_hits=hits,
                tp5_hit_rate=(hits / len(member_ids)) if member_ids else None,
                median_tp5_hours=_median(hit_times),
                p75_tp5_hours=_percentile(hit_times, 0.75),
                median_pre_tp5_adverse=_median(adverse),
                p75_pre_tp5_adverse=_percentile(adverse, 0.75),
            )
        )

    profiled = sum(item.behavior_class != INSUFFICIENT_CLASS for item in profiles if item.episode_id in signals)
    insufficient = len(signals) - profiled
    # Preserve signals with no joined history as implicit insufficient observations.
    insufficient = max(insufficient, 0)
    return TokenRegimeResearchSummary(
        lookback_days=REGIME_LOOKBACK_DAYS,
        minimum_paired_returns=REGIME_MIN_PAIRED_RETURNS,
        profiled_signals=profiled,
        insufficient_signals=insufficient,
        discovery_profiled_signals=discovery_count,
        follower_score_max=follower_max,
        episodic_score_min=episodic_min,
        buckets=tuple(buckets),
        profiles=profiles,
    )
