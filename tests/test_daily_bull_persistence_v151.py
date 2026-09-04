from __future__ import annotations

import asyncio
from datetime import timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    DAILY_BULL_PERSISTENCE_V1_FREEZE_AT,
    _daily_bull_persistence_v1_state,
    build_research_analytics,
    research_signal_dataset_csv,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _row(episode_id: int, *, persistence: bool, target: bool = True):
    row = _base_row(episode_id, "standard")
    row["run_score"] = 3  # explicitly outside Continuation Core V1
    row["hours_run_to_breakdown"] = 5.0 if persistence else 12.0
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    row["feature_snapshot"].update(
        {
            "distance_above_ema20_atr_4h": 2.4,
            "cross_section_percentile": 0.94,
            "previous_momentum_1h": 0.01,
            "daily_close_above_ema20": True,
            "daily_ema20_slope": 0.08,
            "daily_momentum_3d": 0.70,
            "daily_distance_above_ema20_atr": 4.8,
        }
    )
    if target:
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=2)
    return row


def test_persistence_v1_targets_fast_breakdown_inside_extreme_daily_bull_only():
    flagged = _row(1, persistence=True)
    assert _daily_bull_persistence_v1_state(flagged) is True

    slow = _row(2, persistence=False)
    assert _daily_bull_persistence_v1_state(slow) is False

    core = _row(3, persistence=True)
    core["run_score"] = 5
    core["feature_snapshot"]["distance_above_ema20_atr_4h"] = 3.5
    core["feature_snapshot"]["cross_section_percentile"] = 0.995
    assert _daily_bull_persistence_v1_state(core) is False


def test_persistence_v1_replay_is_additional_skip_not_live_replacement():
    flagged = _row(11, persistence=True, target=False)
    safe = _row(12, persistence=False, target=True)
    report = build_research_analytics(
        [flagged, safe],
        generated_at=max(flagged["confirmed_at"], safe["confirmed_at"]) + timedelta(days=1),
    )
    p = report.volatility.daily_bull_persistence
    assert p.flagged_validation.sample == 1
    assert p.portfolio_baseline.eligible_signals == 2
    assert p.portfolio_skip_flagged.eligible_signals == 1
    assert p.portfolio_skip_flagged.entered == 1


def test_persistence_v1_true_forward_starts_strictly_after_freeze_and_exports_state():
    before = _row(21, persistence=True)
    after = _row(22, persistence=True)
    before["confirmed_at"] = DAILY_BULL_PERSISTENCE_V1_FREEZE_AT - timedelta(minutes=1)
    after["confirmed_at"] = DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(minutes=1)
    before["target_5_at"] = before["confirmed_at"] + timedelta(hours=1)
    after["target_5_at"] = after["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        [before, after], generated_at=DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(days=1)
    )
    p = report.volatility.daily_bull_persistence
    assert p.prospective_computable_signals == 1
    assert p.prospective_flagged_validation.sample == 1

    csv_text = research_signal_dataset_csv([before, after], generated_at=DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(days=1)).decode()
    assert "daily_bull_persistence_v1_flagged" in csv_text
    assert "daily_bull_persistence_v1_true_forward" in csv_text


def test_persistence_v1_discord_card_marks_rule_research_only():
    row = _row(31, persistence=True)
    row["confirmed_at"] = DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(minutes=1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        [row], generated_at=DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(days=1)
    )
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    assert asyncio.run(notifier.send_research_analytics(report))
    import json
    embeds = []
    for _, payload in fake.posts:
        if isinstance(payload, dict) and "embeds" in payload:
            embeds.extend(payload["embeds"])
        elif isinstance(payload, dict) and payload.get("data"):
            embeds.extend(json.loads(payload["data"]["payload_json"])["embeds"])
    text = "\n".join(
        embed.get("title", "") + "\n" +
        "\n".join(field.get("name", "") + " " + field.get("value", "") for field in embed.get("fields", []))
        for embed in embeds
    )
    assert "First-Entry Trend Persistence • V1" in text
    assert any("Promoted in v1.3.52" in embed.get("description", "") for embed in embeds)
    for embed in embeds:
        notifier._validate_discord_embed(embed)
