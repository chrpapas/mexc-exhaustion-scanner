from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import build_research_analytics
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _row(episode_id: int, *, flagged: bool, seven_day_return: float):
    row = _base_row(episode_id, "high_risk")
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    # Core V1 on/off.
    row["feature_snapshot"]["distance_above_ema20_atr_4h"] = 3.5 if flagged else 2.0
    # Daily Bull V1 on for every row; therefore Daily-Confirmed Core == Core here.
    row["feature_snapshot"]["daily_close_above_ema20"] = True
    row["feature_snapshot"]["daily_ema20_slope"] = 0.01
    row["feature_snapshot"]["daily_momentum_3d"] = 0.03
    row["return_168h_pct"] = seven_day_return
    row["path_return_168h"] = seven_day_return
    return row


def test_7d_daily_confirmed_core_sizing_reduces_flagged_loser_impact_and_skip_excludes_it():
    flagged_loser = _row(101, flagged=True, seven_day_return=-0.80)
    clean_winner = _row(102, flagged=False, seven_day_return=0.10)
    # Keep timestamps distinct but both fully matured beyond 168h.
    clean_winner["confirmed_at"] = flagged_loser["confirmed_at"] + timedelta(hours=1)
    generated_at = clean_winner["confirmed_at"] + timedelta(days=8)

    report = build_research_analytics([flagged_loser, clean_winner], generated_at=generated_at)
    v = report.volatility

    fixed = v.hold_7d_daily_core_portfolio_fixed
    derisk = v.hold_7d_daily_core_portfolio_de_risked
    skip = v.hold_7d_daily_core_portfolio_skip_flagged

    assert fixed.eligible_signals == 2
    assert fixed.entered == 2
    assert derisk.eligible_signals == 2
    assert derisk.entered == 2
    assert derisk.marked_return > fixed.marked_return
    assert skip.eligible_signals == 1
    assert skip.entered == 1
    assert "skip_daily_confirmed_core" in skip.strategy


def test_7d_daily_core_card_is_emitted_and_discord_valid():
    row = _row(201, flagged=True, seven_day_return=-0.20)
    report = build_research_analytics([row], generated_at=row["confirmed_at"] + timedelta(days=8))
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    assert asyncio.run(notifier.send_research_analytics(report))

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
    assert "7D Hold • Daily-Confirmed Core Replay" in text
    assert "7D + Daily-Confirmed Core sizing" in text
    assert "7D + skip Daily-Confirmed Core" in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
