from __future__ import annotations

import asyncio

import pytest
from datetime import timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    DAILY_CORE_EXPOSURE_SHADOW_V1_FREEZE_AT,
    build_research_analytics,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _safe_row(episode_id: int, minutes: int = 0):
    row = _base_row(episode_id, "high_risk")
    row["confirmed_at"] = DAILY_CORE_EXPOSURE_SHADOW_V1_FREEZE_AT + timedelta(minutes=minutes)
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    # Core false => Daily-Confirmed Core false, while daily state stays computable.
    row["feature_snapshot"].update({
        "run_score": 4,
        "distance_above_ema20_atr_4h": 3.5,
        "previous_momentum_1h": 0.02,
        "cross_section_percentile": 0.995,
        "daily_close_above_ema20": True,
        "daily_ema20_slope": 0.01,
        "daily_momentum_3d": 0.03,
    })
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=2)
    return row


def _flagged_row(episode_id: int, minutes: int = 0):
    row = _safe_row(episode_id, minutes)
    row["feature_snapshot"]["run_score"] = 5
    return row


def test_retrospective_exposure_challengers_change_only_sizing_capacity():
    # Six simultaneous safe signals: live 6x5 can admit all, 5-slot challengers only five.
    rows = [_safe_row(i, 1) for i in range(1, 7)]
    report = build_research_analytics(
        rows,
        generated_at=DAILY_CORE_EXPOSURE_SHADOW_V1_FREEZE_AT + timedelta(days=1),
    )
    v = report.volatility
    live = v.daily_confirmed_core_portfolio_skip_flagged
    mid = v.daily_confirmed_core_portfolio_skip_5x75
    aggressive = v.daily_confirmed_core_portfolio_skip_5x10

    assert live.entered == 6
    assert mid.entered == 5
    assert aggressive.entered == 5
    assert live.max_observed_exposure_pct == pytest.approx(0.30)
    assert mid.max_observed_exposure_pct == pytest.approx(0.375)
    assert aggressive.max_observed_exposure_pct == pytest.approx(0.50)
    assert aggressive.marked_return > mid.marked_return > 0


def test_exposure_shadow_true_forward_starts_strictly_after_freeze_and_hard_filters():
    before = _safe_row(11, -1)
    safe_after = _safe_row(12, 1)
    flagged_after = _flagged_row(13, 2)
    report = build_research_analytics(
        [before, safe_after, flagged_after],
        generated_at=DAILY_CORE_EXPOSURE_SHADOW_V1_FREEZE_AT + timedelta(days=1),
    )
    v = report.volatility
    assert v.prospective_daily_core_exposure_shadow_computable_signals == 2
    assert v.prospective_daily_core_exposure_shadow_flagged_signals == 1
    assert v.prospective_daily_core_exposure_shadow_allowed_signals == 1
    assert v.prospective_daily_core_exposure_shadow_6x5.eligible_signals == 1
    assert v.prospective_daily_core_exposure_shadow_5x10.eligible_signals == 1
    assert v.prospective_daily_core_exposure_shadow_6x5.entered == 1
    assert v.prospective_daily_core_exposure_shadow_5x10.entered == 1


def test_research_discord_includes_exposure_shadow_card():
    row = _safe_row(21, 1)
    report = build_research_analytics(
        [row], generated_at=DAILY_CORE_EXPOSURE_SHADOW_V1_FREEZE_AT + timedelta(days=1)
    )
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    assert asyncio.run(notifier.send_research_analytics(report))
    titles = []
    import json
    for _, payload in fake.posts:
        if isinstance(payload, dict) and "embeds" in payload:
            titles.extend(embed.get("title", "") for embed in payload["embeds"])
        elif isinstance(payload, dict) and payload.get("data"):
            embeds = json.loads(payload["data"]["payload_json"])["embeds"]
            titles.extend(embed.get("title", "") for embed in embeds)
    assert "📐 Exposure Shadow • Hard Filter" in titles
