from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    RESEARCH_OOS_FREEZE_AT,
    build_research_analytics,
    entry_gate_v1,
    research_entry_research_csv,
    research_signal_dataset_csv,
    research_strategy_sweeps_csv,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _continuation_row(episode_id: int) -> dict:
    row = _base_row(episode_id)
    row["run_score"] = 6
    row["exhaustion_score"] = 6
    row["hours_run_to_breakdown"] = 30.0
    row["hours_episode_to_confirmation"] = 31.0
    row["feature_snapshot"].update(
        {
            "volume_zscore_15m": 2.0,
            "funding_rate": 0.001,
            "distance_above_ema20_atr_4h": 4.0,
            "momentum_1h": -0.01,
        }
    )
    return row


def test_entrygate_v1_uses_frozen_quality_and_continuation_boundaries():
    assert entry_gate_v1(_base_row(1))
    assert not entry_gate_v1(_continuation_row(2))


def test_four_way_replay_filters_entrygate_without_changing_unfiltered_portfolios():
    good = _base_row(1)
    bad = _continuation_row(2)
    for row in (good, bad):
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
        row["target_20_at"] = row["confirmed_at"] + timedelta(hours=2)
        row["target_20_path_at"] = row["target_20_at"]

    report = build_research_analytics(
        [good, bad], generated_at=datetime(2026, 8, 20, tzinfo=UTC)
    )

    assert report.portfolio_tp5.signals == 2
    assert report.portfolio_tp5.eligible_signals == 2
    assert report.portfolio_tp5.filtered_entry_gate == 0
    assert report.portfolio_entrygate_tp5.signals == 2
    assert report.portfolio_entrygate_tp5.eligible_signals == 1
    assert report.portfolio_entrygate_tp5.filtered_entry_gate == 1
    assert report.portfolio_entrygate_tp5.entered == 1


def test_tp5_portfolio_mtm_uses_stored_15m_close_marks():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=2)
    row["path_mae_before_target_5"] = -0.63
    row["path_mae_before_target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    path = [
        {
            "episode_id": 1,
            "candle_close_at": row["confirmed_at"] + timedelta(minutes=15),
            "close_return_pct": -0.20,
        },
        {
            "episode_id": 1,
            "candle_close_at": row["confirmed_at"] + timedelta(hours=1),
            "close_return_pct": -0.50,
        },
        {
            "episode_id": 1,
            "candle_close_at": row["confirmed_at"] + timedelta(hours=2),
            "close_return_pct": 0.04,
        },
    ]

    report = build_research_analytics(
        [row],
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        portfolio_path_rows=path,
    )
    p = report.portfolio_tp5

    assert p.mtm_points >= 3
    assert p.max_mtm_drawdown is not None and p.max_mtm_drawdown > 0.024
    assert p.max_unrealized_loss is not None and abs(p.max_unrealized_loss - 0.025) < 1e-12
    assert p.max_simultaneous_losers == 1
    assert p.worst_trade_episode_id == 1
    assert p.worst_trade_pre_target_mae == 0.63
    assert p.portfolio_return_at_worst_trade_mae is not None
    assert p.portfolio_return_at_worst_trade_mae < -0.024


def test_oos_freeze_splits_discovery_and_post_freeze_by_confirmation_time():
    discovery = _base_row(1)
    discovery["confirmed_at"] = RESEARCH_OOS_FREEZE_AT - timedelta(hours=1)
    discovery["path_last_at"] = discovery["confirmed_at"] + timedelta(hours=336)
    discovery["target_5_at"] = discovery["confirmed_at"] + timedelta(hours=1)

    post = _base_row(2)
    post["confirmed_at"] = RESEARCH_OOS_FREEZE_AT + timedelta(hours=1)
    post["path_last_at"] = post["confirmed_at"] + timedelta(hours=336)
    post["target_5_at"] = post["confirmed_at"] + timedelta(hours=2)
    post["path_mae_before_target_5"] = -0.12

    report = build_research_analytics(
        [discovery, post],
        generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=10),
    )
    cohorts = {item.cohort: item for item in report.prospective_cohorts}

    assert cohorts["discovery"].signals == 1
    assert cohorts["post_freeze"].signals == 1
    assert cohorts["post_freeze"].complete_7d == 1
    assert cohorts["post_freeze"].tp5_hits == 1
    assert cohorts["post_freeze"].tp5_hit_rate == 1.0
    assert any(item.cohort == "post_freeze" and item.sample == 1 for item in report.prospective_score_buckets)


def test_v133_csv_exports_entrygate_oos_and_mtm_portfolios():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))

    strategy = research_strategy_sweeps_csv(report).decode()
    entry = research_entry_research_csv(report).decode()
    dataset = research_signal_dataset_csv([row]).decode()

    assert "entrygate_v1__current_live_5standard_1high" in strategy
    assert "entrygate_v1__tp5_challenger_6x5pct" in strategy
    assert "prospective_oos_cohort" in strategy
    assert "prospective_score_bucket" in entry
    assert "entry_gate_v1_eligible" in dataset.splitlines()[0]


def test_v133_notifier_reports_four_way_replay_and_prospective_freeze():
    row = _base_row(1)
    row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    path = [{
        "episode_id": 1,
        "candle_close_at": row["confirmed_at"] + timedelta(minutes=15),
        "close_return_pct": -0.10,
    }]
    report = build_research_analytics(
        [row], generated_at=datetime(2026, 8, 20, tzinfo=UTC), portfolio_path_rows=path
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
        embed.get("title", "") + "\n" + embed.get("description", "") + "\n" +
        "\n".join(field.get("name", "") + " " + field.get("value", "") for field in embed.get("fields", []))
        for embed in embeds
    )
    assert "Strategy Validation" in text
    assert "TP5 indefinite" in text
    assert "TP5 + SL75" in text
    assert "TP5 + 7D cutoff" in text
    assert "Forward Validation" in text
    assert "EntryGate-v1" not in text
    assert "Post-freeze score buckets" not in text
    assert "entrygate_v1__tp5_challenger_6x5pct" not in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
