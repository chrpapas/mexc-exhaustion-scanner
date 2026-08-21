from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    RESEARCH_OOS_FREEZE_AT,
    build_research_analytics,
    research_entry_research_csv,
    research_strategy_sweeps_csv,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _rebase(row: dict, confirmed: datetime) -> dict:
    row = dict(row)
    row["confirmed_at"] = confirmed
    row["path_last_at"] = confirmed + timedelta(hours=336)
    row["path_mfe_at"] = confirmed + timedelta(hours=120)
    row["path_mae_at"] = confirmed + timedelta(hours=10)
    row["path_mfe_14d_at"] = confirmed + timedelta(hours=250)
    row["path_mae_14d_at"] = confirmed + timedelta(hours=200)
    return row


def test_prospective_tp5_live_tracker_resolves_hits_waiting_and_7d_failures():
    freeze = RESEARCH_OOS_FREEZE_AT
    hit = _rebase(_base_row(1), freeze + timedelta(hours=1))
    hit["target_5_at"] = hit["confirmed_at"] + timedelta(hours=2)
    hit["path_mae_before_target_5"] = -0.12
    hit["path_last_at"] = hit["confirmed_at"] + timedelta(hours=4)
    hit["return_168h_pct"] = None

    waiting = _rebase(_base_row(2), freeze + timedelta(hours=2))
    waiting["target_5_at"] = None
    waiting["path_last_at"] = waiting["confirmed_at"] + timedelta(hours=24)
    waiting["return_168h_pct"] = None

    failed = _rebase(_base_row(3), freeze + timedelta(hours=3))
    failed["target_5_at"] = None
    failed["path_last_at"] = failed["confirmed_at"] + timedelta(hours=168)
    failed["return_168h_pct"] = -0.10

    path = [
        {
            "episode_id": waiting["episode_id"],
            "candle_close_at": waiting["confirmed_at"] + timedelta(hours=8),
            "close_return_pct": -0.35,
        }
    ]
    report = build_research_analytics(
        [hit, waiting, failed],
        generated_at=freeze + timedelta(days=8),
        portfolio_path_rows=path,
    )
    live = report.prospective_tp5_live
    assert (live.signals, live.hits, live.waiting, live.failed, live.resolved) == (3, 1, 1, 1, 2)
    assert live.resolved_hit_rate == 0.5
    assert live.median_hit_hours == 2.0
    assert live.worst_pre_hit_adverse == 0.12
    assert live.worst_waiting_close_adverse == 0.35


def test_prospective_entrygate_acceptance_reports_total_and_rolling_20():
    rows = []
    for i in range(25):
        row = _rebase(_base_row(100 + i), RESEARCH_OOS_FREEZE_AT + timedelta(minutes=15 * (i + 1)))
        row["return_168h_pct"] = None
        row["path_last_at"] = row["confirmed_at"] + timedelta(hours=1)
        if i % 2:
            row["run_score"] = 6
            row["exhaustion_score"] = 6
            row["feature_snapshot"] = dict(row["feature_snapshot"])
            row["feature_snapshot"]["volume_zscore_15m"] = 2.0
            row["feature_snapshot"]["funding_rate"] = 0.001
            row["feature_snapshot"]["momentum_1h"] = -0.01
        rows.append(row)
    report = build_research_analytics(rows, generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=1))
    gate = report.prospective_gate_acceptance
    assert gate.signals == 25
    assert gate.rolling_signals == 20
    assert 0 < gate.eligible < 25
    assert 0 < gate.rolling_eligible < 20
    assert gate.eligible_rate is not None
    assert gate.rolling_eligible_rate is not None


def test_regime_drift_uses_frozen_discovery_quartiles_and_all_postfreeze_signals():
    discovery = []
    for i, exhaustion in enumerate((2, 4, 6, 8), start=1):
        row = _rebase(_base_row(i), RESEARCH_OOS_FREEZE_AT - timedelta(hours=10 - i))
        row["exhaustion_score"] = exhaustion
        discovery.append(row)
    post = []
    for i, exhaustion in enumerate((9, 10), start=10):
        row = _rebase(_base_row(i), RESEARCH_OOS_FREEZE_AT + timedelta(hours=i))
        row["exhaustion_score"] = exhaustion
        row["return_168h_pct"] = None
        row["path_last_at"] = row["confirmed_at"] + timedelta(hours=1)
        post.append(row)
    report = build_research_analytics(
        discovery + post,
        generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=1),
    )
    item = next(x for x in report.prospective_regime_drift if x.feature == "exhaustion_score")
    assert item.discovery_sample == 4
    assert item.post_freeze_sample == 2
    assert item.discovery_median == 5.0
    assert item.post_freeze_median == 9.5
    assert item.post_above_discovery_p75_rate == 1.0


def test_postfreeze_portfolio_table_excludes_discovery_complete_paths():
    discovery = _rebase(_base_row(1), RESEARCH_OOS_FREEZE_AT - timedelta(days=8))
    discovery["target_5_at"] = discovery["confirmed_at"] + timedelta(hours=1)
    post = _rebase(_base_row(2), RESEARCH_OOS_FREEZE_AT + timedelta(hours=1))
    post["target_5_at"] = post["confirmed_at"] + timedelta(hours=1)
    post["target_20_at"] = post["confirmed_at"] + timedelta(hours=2)
    post["target_20_path_at"] = post["target_20_at"]
    report = build_research_analytics(
        [discovery, post], generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=10)
    )
    assert len(report.prospective_portfolios) == 4
    assert all(item.signals == 1 for item in report.prospective_portfolios)


def test_v134_csv_exports_fast_monitor_gate_drift_and_postfreeze_portfolios():
    post = _rebase(_base_row(1), RESEARCH_OOS_FREEZE_AT + timedelta(hours=1))
    post["target_5_at"] = post["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        [post], generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=8)
    )
    strategy = research_strategy_sweeps_csv(report).decode()
    entry = research_entry_research_csv(report).decode()
    assert "prospective_tp5_live" in strategy
    assert "prospective_entrygate_acceptance" in strategy
    assert "prospective_portfolio_replay_paired_7d" in strategy
    assert "prospective_regime_drift" in entry


def test_v134_notifier_adds_prospective_monitor_embed():
    post = _rebase(_base_row(1), RESEARCH_OOS_FREEZE_AT + timedelta(hours=1))
    post["target_5_at"] = post["confirmed_at"] + timedelta(hours=1)
    post["path_mae_before_target_5"] = -0.10
    report = build_research_analytics(
        [post], generated_at=RESEARCH_OOS_FREEZE_AT + timedelta(days=8)
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
    assert "Prospective Monitor" in text
    assert "TP5 live tracker" in text
    assert "EntryGate-v1 acceptance" in text
    assert "Regime drift" in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
