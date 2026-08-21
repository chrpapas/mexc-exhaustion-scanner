from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import build_research_analytics, research_strategy_sweeps_csv
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _at(row: dict, confirmed: datetime) -> dict:
    row = dict(row)
    row["confirmed_at"] = confirmed
    row["path_last_at"] = confirmed + timedelta(hours=336)
    row["path_mfe_at"] = confirmed + timedelta(hours=120)
    row["path_mae_at"] = confirmed + timedelta(hours=10)
    row["path_mfe_14d_at"] = confirmed + timedelta(hours=250)
    row["path_mae_14d_at"] = confirmed + timedelta(hours=200)
    return row


def test_calendar_throughput_uses_immature_signals_and_exposes_slot_recycling():
    start = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(8):
        row = _at(_base_row(100 + i, "standard"), start + timedelta(hours=2 * i))
        row["return_168h_pct"] = None  # deliberately immature for strict paired 7d research
        row["target_5_at"] = row["confirmed_at"] + timedelta(minutes=30)
        row["path_latest_return"] = 0.05
        rows.append(row)

    report = build_research_analytics(rows, generated_at=start + timedelta(days=2))
    calendar = report.calendar_throughput

    assert report.baseline.matured_7d == 0
    assert calendar.current.signals == 8
    assert calendar.current.entered == 5
    assert calendar.current.open_positions == 5
    assert calendar.current.missed_capacity == 3
    assert calendar.tp5.signals == 8
    assert calendar.tp5.entered == 8
    assert calendar.tp5.closed == 8
    assert calendar.tp5.open_positions == 0
    assert calendar.tp5.missed_capacity == 0


def test_true_30d_replay_is_withheld_until_thirty_calendar_days_exist():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _at(_base_row(1), start)
    row["target_5_at"] = start + timedelta(hours=1)
    report = build_research_analytics([row], generated_at=start + timedelta(days=12))

    calendar = report.calendar_throughput
    assert calendar.latest_30d_current is None
    assert calendar.latest_30d_tp5 is None
    assert abs(calendar.days_until_30d - 18.0) < 1e-12


def test_latest_30d_empty_book_replay_uses_only_signals_inside_window():
    generated = datetime(2026, 9, 10, tzinfo=UTC)
    old = _at(_base_row(1), generated - timedelta(days=31))
    a = _at(_base_row(2), generated - timedelta(days=29))
    b = _at(_base_row(3), generated - timedelta(days=1))
    for row in (old, a, b):
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)

    report = build_research_analytics([old, a, b], generated_at=generated)
    calendar = report.calendar_throughput

    assert calendar.latest_30d_current is not None
    assert calendar.latest_30d_tp5 is not None
    assert calendar.latest_30d_current.signals == 2
    assert calendar.latest_30d_tp5.signals == 2
    assert calendar.latest_30d_tp5.entered == 2
    assert calendar.latest_30d_tp5.closed == 2


def test_v135_strategy_csv_exports_calendar_throughput_and_true_30d_rows():
    generated = datetime(2026, 9, 10, tzinfo=UTC)
    old = _at(_base_row(1), generated - timedelta(days=31))
    new = _at(_base_row(2), generated - timedelta(days=1))
    old["target_5_at"] = old["confirmed_at"] + timedelta(hours=1)
    new["target_5_at"] = new["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics([old, new], generated_at=generated)

    csv_text = research_strategy_sweeps_csv(report).decode()
    assert "calendar_throughput_observed" in csv_text
    assert "calendar_latest_30d_empty_book" in csv_text


def test_v135_notifier_adds_calendar_throughput_board_without_short_window_projection():
    start = datetime(2026, 8, 21, tzinfo=UTC)
    row = _at(_base_row(1), start)
    row["return_168h_pct"] = None
    row["target_5_at"] = start + timedelta(hours=1)
    report = build_research_analytics([row], generated_at=start + timedelta(days=2))

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
    assert "Calendar Throughput" in text
    assert "No short-window return is extrapolated" in text
    assert "entries/day" in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
