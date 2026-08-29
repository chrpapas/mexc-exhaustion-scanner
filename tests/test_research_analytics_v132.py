from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import build_research_analytics, research_strategy_sweeps_csv
from tests.test_research_analytics_v128 import _base_row


def test_tp5_pre_hit_mae_and_adverse_races_use_all_observed_signals():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=2)
    row["path_mae_before_target_5"] = -0.18
    row["path_mae_before_target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    row["adverse_10_at"] = row["confirmed_at"] + timedelta(hours=1)
    row["adverse_20_at"] = None
    row["adverse_30_at"] = row["target_5_at"]  # same 15m candle is intentionally ambiguous
    row["adverse_50_at"] = None
    row["adverse_75_at"] = None
    row["adverse_100_at"] = None

    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    risk = report.tp5_risk
    assert risk.sample == 1
    assert risk.hits == 1
    assert risk.hit_rate == 1.0
    assert risk.median_time_hours == 2.0
    assert risk.median_adverse_before_target == 0.18

    race10 = next(x for x in risk.adverse_races if x.adverse_threshold_pct == 10)
    race20 = next(x for x in risk.adverse_races if x.adverse_threshold_pct == 20)
    race30 = next(x for x in risk.adverse_races if x.adverse_threshold_pct == 30)
    assert race10.adverse_first == 1
    assert race20.target_first == 1
    assert race30.same_candle == 1


def test_tp5_fast_first_candle_hit_counts_zero_observed_pre_hit_adverse():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(minutes=15)
    row["path_mae_before_target_5"] = None

    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    assert report.tp5_risk.worst_adverse_before_target == 0.0


def test_tp5_challenger_recycles_generic_slots_while_current_high_risk_slot_stays_busy():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = []
    for idx in range(6):
        row = _base_row(idx + 1, "high_risk")
        row["confirmed_at"] = start + timedelta(hours=idx * 2)
        row["path_last_at"] = row["confirmed_at"] + timedelta(hours=336)
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
        row["target_20_at"] = None
        row["target_20_path_at"] = None
        row["path_return_96h"] = -0.10
        rows.append(row)

    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    current = report.portfolio_current
    tp5 = report.portfolio_tp5

    assert current.signals == tp5.signals == 6
    assert current.entered == 1
    assert current.missed_capacity == 5
    assert tp5.entered == 6
    assert tp5.closed == 6
    assert tp5.missed_capacity == 0
    assert tp5.max_open_positions == 1
    assert tp5.marked_return > 0


def test_tp5_challenger_enforces_same_symbol_rule():
    a = _base_row(1)
    b = _base_row(2)
    b["symbol"] = a["symbol"]
    b["confirmed_at"] = a["confirmed_at"] + timedelta(hours=1)
    b["path_last_at"] = b["confirmed_at"] + timedelta(hours=336)
    a["target_5_at"] = a["confirmed_at"] + timedelta(hours=3)
    b["target_5_at"] = b["confirmed_at"] + timedelta(hours=1)

    report = build_research_analytics([a, b], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    assert report.portfolio_tp5.entered == 1
    assert report.portfolio_tp5.missed_same_symbol == 1


def test_strategy_csv_contains_tp5_risk_and_portfolio_rows():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    row["path_mae_before_target_5"] = -0.05
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    text = research_strategy_sweeps_csv(report).decode("utf-8")
    assert "tp5_pre_hit_summary_observed" in text
    assert "tp5_adverse_race_observed" in text
    assert "portfolio_replay_paired_7d" in text
    assert "tp5_challenger_6x5pct" in text
    assert "current_live_5standard_1high" in text


def test_tp5_validation_counts_target_after_day_7_as_hit():
    row = _base_row(77)
    row["target_5_at"] = row["confirmed_at"] + timedelta(days=10)
    row["path_mae_before_target_5"] = -0.42
    generated_at = row["confirmed_at"] + timedelta(days=12)

    report = build_research_analytics([row], generated_at=generated_at)

    assert report.tp5_risk.sample == 1
    assert report.tp5_risk.hits == 1
    assert report.tp5_risk.hit_rate == 1.0
    assert report.tp5_risk.median_time_hours == 240.0


def test_research_notifier_emits_tp5_challenger_card_within_discord_limits():
    import asyncio

    from app.notifier import DiscordNotifier
    from tests.test_notifier import _FakeClient

    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    row["path_mae_before_target_5"] = -0.12
    for threshold in (10, 20, 30, 50, 75, 100):
        row[f"adverse_{threshold}_at"] = None
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))

    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    sent = asyncio.run(notifier.send_research_analytics(report))

    assert sent
    embeds = []
    for _, payload in fake.posts:
        if isinstance(payload, dict) and "embeds" in payload:
            embeds.extend(payload["embeds"])
        elif isinstance(payload, dict) and "data" in payload and payload["data"]:
            import json
            embeds.extend(json.loads(payload["data"]["payload_json"])["embeds"])
    all_text = "\n".join(
        embed.get("title", "") + "\n" + embed.get("description", "") + "\n" + "\n".join(
            field.get("name", "") + " " + field.get("value", "")
            for field in embed.get("fields", [])
        )
        for embed in embeds
    )
    assert "Strategy Validation" in all_text
    assert "TP5 indefinite" in all_text
    assert "TP5 + SL75" in all_text
    assert "TP5 + 7D cutoff" in all_text
    assert "TP1-10" not in all_text
    assert "TP2-6" not in all_text
    assert "TP2-10" not in all_text
    assert "EntryGate" not in all_text
    for embed in embeds:
        assert notifier._discord_embed_char_count(embed) <= 6000
