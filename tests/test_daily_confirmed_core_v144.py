from __future__ import annotations

import asyncio
from datetime import timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    DAILY_CONFIRMED_CORE_V1_FREEZE_AT,
    build_research_analytics,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _row(episode_id: int, *, core: bool, daily_bull: bool, outcome: str):
    row = _base_row(episode_id, "high_risk")
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    row["feature_snapshot"]["distance_above_ema20_atr_4h"] = 3.2 if core else 2.0
    row["feature_snapshot"]["daily_close_above_ema20"] = daily_bull
    row["feature_snapshot"]["daily_ema20_slope"] = 0.01 if daily_bull else -0.01
    row["feature_snapshot"]["daily_momentum_3d"] = 0.03 if daily_bull else -0.02
    if outcome == "target":
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=2)
    elif outcome == "stop":
        row["adverse_75_at"] = row["confirmed_at"] + timedelta(hours=3)
    return row


def test_daily_confirmed_core_v1_is_exact_core_and_daily_intersection():
    rows = [
        _row(1, core=False, daily_bull=False, outcome="target"),
        _row(2, core=False, daily_bull=True, outcome="target"),
        _row(3, core=True, daily_bull=False, outcome="target"),
        _row(4, core=True, daily_bull=True, outcome="stop"),
    ]
    report = build_research_analytics(
        rows,
        generated_at=max(row["confirmed_at"] for row in rows) + timedelta(days=1),
    )
    v = report.volatility
    assert v.daily_confirmed_core_computable_signals == 4
    assert v.daily_confirmed_core_missing_signals == 0
    assert v.daily_confirmed_core_flagged_validation.sample == 1
    assert v.daily_confirmed_core_flagged_validation.stop_exits == 1
    assert v.daily_confirmed_core_unflagged_validation.sample == 3
    assert v.daily_confirmed_core_portfolio_de_risked.eligible_signals == 4
    assert v.daily_confirmed_core_portfolio_skip_flagged.eligible_signals == 3
    assert v.daily_confirmed_core_portfolio_skip_flagged.entered == 3
    assert (
        v.daily_confirmed_core_portfolio_skip_flagged.marked_return
        > v.daily_confirmed_core_portfolio_de_risked.marked_return
    )


def test_daily_confirmed_core_true_forward_starts_after_matrix_freeze():
    before = _row(11, core=True, daily_bull=True, outcome="target")
    after = _row(12, core=True, daily_bull=True, outcome="target")
    before["confirmed_at"] = DAILY_CONFIRMED_CORE_V1_FREEZE_AT - timedelta(minutes=1)
    after["confirmed_at"] = DAILY_CONFIRMED_CORE_V1_FREEZE_AT + timedelta(minutes=1)
    before["target_5_at"] = before["confirmed_at"] + timedelta(hours=1)
    after["target_5_at"] = after["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        [before, after],
        generated_at=DAILY_CONFIRMED_CORE_V1_FREEZE_AT + timedelta(days=1),
    )
    v = report.volatility
    assert v.prospective_daily_confirmed_core_computable_signals == 1
    assert v.prospective_daily_confirmed_core_flagged_validation.sample == 1
    assert v.prospective_daily_confirmed_core_flagged_validation.target_exits == 1


def test_daily_regime_card_includes_daily_confirmed_core_replay_and_forward_block():
    row = _row(21, core=True, daily_bull=True, outcome="target")
    row["confirmed_at"] = DAILY_CONFIRMED_CORE_V1_FREEZE_AT + timedelta(minutes=1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        [row], generated_at=DAILY_CONFIRMED_CORE_V1_FREEZE_AT + timedelta(days=1)
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
    assert "Daily-Confirmed Core V1 • retrospective replay" in text
    assert "Daily-Confirmed Core V1 • true forward" in text
    assert "Daily-Confirmed Core V1 • hard skip" in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
