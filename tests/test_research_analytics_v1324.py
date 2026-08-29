from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json

from app.notifier import DiscordNotifier
from app.research_analytics import build_research_analytics, research_strategy_sweeps_csv
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def test_tp5_sl75_challenger_stops_before_later_tp5():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(1, "high_risk")
    row["confirmed_at"] = start
    row["path_last_at"] = start + timedelta(hours=336)
    row["adverse_75_at"] = start + timedelta(hours=4)
    row["target_5_at"] = start + timedelta(hours=12)
    report = build_research_analytics([row], generated_at=start + timedelta(days=14))
    sl75 = report.portfolio_tp5_sl75
    no_stop = report.portfolio_tp5
    assert sl75.strategy == "tp5_sl75_challenger_6x5pct"
    assert sl75.entered == 1 and sl75.closed == 1
    assert sl75.realized_return < 0
    assert no_stop.realized_return > 0


def test_tp5_sl75_challenger_prefers_tp5_when_target_is_first():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(2, "standard")
    row["confirmed_at"] = start
    row["path_last_at"] = start + timedelta(hours=336)
    row["target_5_at"] = start + timedelta(hours=1)
    row["adverse_75_at"] = start + timedelta(hours=6)
    report = build_research_analytics([row], generated_at=start + timedelta(days=14))
    assert report.portfolio_tp5_sl75.realized_return > 0


def test_tp5_sl75_same_candle_is_conservative_stop_first():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(3, "high_risk")
    row["confirmed_at"] = start
    row["path_last_at"] = start + timedelta(hours=336)
    row["target_5_at"] = start + timedelta(hours=2)
    row["adverse_75_at"] = row["target_5_at"]
    report = build_research_analytics([row], generated_at=start + timedelta(days=14))
    assert report.portfolio_tp5_sl75.realized_return < 0


def test_strategy_csv_contains_sl75_comparison():
    row = _base_row(4)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    row["adverse_75_at"] = None
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    text = research_strategy_sweeps_csv(report).decode("utf-8")
    assert "tp5_sl75_challenger_6x5pct" in text


def test_strategy_validation_report_shows_sl75_outcome_race():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    winner = _base_row(10, "standard")
    winner["confirmed_at"] = start
    winner["target_5_at"] = start + timedelta(hours=2)
    winner["adverse_75_at"] = None

    stopped = _base_row(11, "high_risk")
    stopped["confirmed_at"] = start + timedelta(hours=1)
    stopped["adverse_75_at"] = stopped["confirmed_at"] + timedelta(hours=3)
    stopped["target_5_at"] = None

    report = build_research_analytics(
        [winner, stopped],
        generated_at=start + timedelta(days=14),
    )
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    assert asyncio.run(notifier.send_research_analytics(report))

    text = ""
    for _, payload in fake.posts:
        if payload.get("data"):
            body = json.loads(payload["data"]["payload_json"])
        else:
            body = payload
        for embed in body.get("embeds", []):
            if embed.get("title") == "🧠 Exhaustion Scanner • Research Intelligence":
                text += embed.get("description", "") + "\n"
                text += "\n".join(
                    field.get("name", "") + " " + field.get("value", "")
                    for field in embed.get("fields", [])
                )

    assert "TP5 + SL75" in text
    assert "TP5 **1**" in text
    assert "SL75 **1**" in text
    assert "7D hold" in text
    assert "open **0**" in text
