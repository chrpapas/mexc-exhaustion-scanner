"""Unreleased reference-only full four-slot V40 VOL30/H4 sizing projection.

Retains the frozen multi-slot admission economics instead of promoting the
one-position 12.50-USDT 1x canary into a misleading 'full strategy'. This
module has ZERO exchange/DB APIs, no trade permit and no risk approval.
Historical 5x shock budgeting is NOT a live leverage recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

from app.t100_200_strategy import STRATEGY_ID, LOW_TIER, HIGH_TIER
from app.t100_live_v40_stage2_source import V40SourceBatch, V40SourceCandidate
from app.t100_v40_admission import admission, causal_49_bar_features, MAX_POSITIONS


class V40FullPortfolioHalted(ValueError):
    pass


@dataclass(frozen=True)
class V40ReferenceEntry:
    signal_id: int
    symbol: str
    tier: str
    desired_notional_usdt: float
    modeled_notional_usdt: float
    p25_turnover_usdt: float
    stressed_exit_capacity_usdt: float


@dataclass(frozen=True)
class V40FullPortfolioProjection:
    status: str
    entries: tuple[V40ReferenceEntry, ...]
    skipped: tuple[tuple[int, str], ...]
    resulting_slot_count: int
    total_modeled_new_notional_usdt: float
    historical_reference_only: bool = True
    native_exchange_execution_verified: bool = False
    risk_leverage_approved: bool = False
    exchange_posts: int = 0
    live_authorization: bool = False


def project_frozen_v40_multi_slot(
    *,
    source: V40SourceBatch,
    equity_usdt: float,
    marked_existing_usdt: float,
    owned_open_symbols: Sequence[str],
    used_same_bar_usdt: float = 0.0,
    cost_bps: float = 100.0,
) -> V40FullPortfolioProjection:
    if (
        type(source) is not V40SourceBatch
        or source.status != "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
        or source.history_complete is not True
        or source.read_only_repeatable_read is not True
        or source.entry_allowed is not False
        or source.live_authorization is not False
        or type(source.candidates) is not tuple
        or type(owned_open_symbols) not in (tuple, list)
        or not 0 <= len(owned_open_symbols) <= MAX_POSITIONS
        or len(set(owned_open_symbols)) != len(owned_open_symbols)
        or any(type(s) is not str or not s.endswith("_USDT") for s in owned_open_symbols)
        or any(
            type(x) is not float or not isfinite(x)
            for x in (equity_usdt, marked_existing_usdt, used_same_bar_usdt, cost_bps)
        )
        or equity_usdt <= 0
        or marked_existing_usdt < 0
        or used_same_bar_usdt < 0
        or cost_bps not in (100., 200.)
    ):
        raise V40FullPortfolioHalted("V40_FULL_REFERENCE_INPUT_UNVERIFIED")

    ordered = sorted(
        source.candidates, key=lambda c: (c.admission_open_ts, c.signal_id)
    )
    if len({c.signal_id for c in ordered}) != len(ordered):
        raise V40FullPortfolioHalted("V40_FULL_REFERENCE_DUPLICATE_SIGNAL")
    if len({c.admission_open_ts for c in ordered}) > 1:
        # A different 30m bar needs a fresh account+same-bar snapshot.
        raise V40FullPortfolioHalted("V40_FULL_REFERENCE_MIXED_SIGNAL_BARS")

    owned = set(owned_open_symbols)
    entries: list[V40ReferenceEntry] = []
    skips: list[tuple[int, str]] = []
    marked = marked_existing_usdt
    already_used = used_same_bar_usdt

    for candidate in ordered:
        if not (
            type(candidate) is V40SourceCandidate
            and candidate.strategy_id == STRATEGY_ID
            and candidate.signal_id > 0
            and candidate.entry_allowed is False
            and candidate.live_authorization is False
            and type(candidate.prior_bars) is tuple
            and len(candidate.prior_bars) == 49
            and candidate.stage1_return_24h >= .15
            and candidate.stage1_atr7_pct >= 4
            and candidate.tier == (
                HIGH_TIER if candidate.stage1_atr7_pct >= 5.7 else LOW_TIER
            )
            and type(candidate.pre24_vol_pct) is float
            and isfinite(candidate.pre24_vol_pct)
            and 0 <= candidate.observed_age_seconds <= 900
        ):
            raise V40FullPortfolioHalted("V40_FULL_REFERENCE_SOURCE_NOT_FROZEN")
        try:
            features = causal_49_bar_features(
                candidate.prior_bars, admit_open_ts=candidate.admission_open_ts
            )
        except Exception:
            raise V40FullPortfolioHalted("V40_FULL_REFERENCE_PRIOR_BARS_INVALID") from None
        if (
            features is None
            or abs(features[0] - candidate.pre24_vol_pct) > 1e-6
        ):
            raise V40FullPortfolioHalted("V40_FULL_REFERENCE_VOL30_DRIFT")

        decision = admission(
            prior_bars=candidate.prior_bars,
            admission_open_ts=candidate.admission_open_ts,
            equity_usdt=equity_usdt,
            marked_existing_usdt=marked,
            stage1_atr7_pct=candidate.stage1_atr7_pct,
            active_positions=len(owned),
            already_open_symbol=candidate.symbol in owned,
            used_same_bar_usdt=already_used,
            cost_bps=cost_bps,
        )
        if decision.action != "ADMIT":
            skips.append((candidate.signal_id, decision.reason))
            continue

        modeled = decision.modeled_notional_usdt
        if not isfinite(modeled) or modeled <= 0:
            raise V40FullPortfolioHalted("V40_FULL_REFERENCE_CAPACITY_INVALID")
        entries.append(V40ReferenceEntry(
            signal_id=candidate.signal_id,
            symbol=candidate.symbol,
            tier=candidate.tier,
            desired_notional_usdt=decision.desired_notional_usdt,
            modeled_notional_usdt=modeled,
            p25_turnover_usdt=float(decision.p25_turnover_usdt),
            stressed_exit_capacity_usdt=decision.stressed_exit_capacity_usdt,
        ))
        owned.add(candidate.symbol)
        already_used += modeled
        marked += modeled

    return V40FullPortfolioProjection(
        status="V40_FROZEN_FOUR_SLOT_HISTORICAL_REFERENCE_ONLY",
        entries=tuple(entries), skipped=tuple(skips),
        resulting_slot_count=len(owned),
        total_modeled_new_notional_usdt=sum(e.modeled_notional_usdt for e in entries),
    )
