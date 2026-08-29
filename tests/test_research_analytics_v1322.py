from __future__ import annotations

import asyncio
from datetime import timedelta

from app.notifier import DiscordNotifier
from app.research_analytics import (
    PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF,
    PERSISTENT_RUN_RISK_FREEZE_AT,
    build_research_analytics,
    persistent_run_risk_flags,
    research_signal_dataset_csv,
)
from tests.test_notifier import _FakeClient
from tests.test_research_analytics_v128 import _base_row


def _risk_row(episode_id: int, *, confirmed_at, run_hours: float, ema_atr: float, breach_hours: float | None = None):
    row = _base_row(episode_id, "high_risk")
    row["confirmed_at"] = confirmed_at
    row["hours_run_to_breakdown"] = run_hours
    row["feature_snapshot"]["distance_above_ema20_atr_4h"] = ema_atr
    row["path_last_at"] = confirmed_at + timedelta(hours=336)
    row["target_5_at"] = confirmed_at + timedelta(hours=1)
    if breach_hours is not None:
        row["adverse_100_at"] = confirmed_at + timedelta(hours=breach_hours)
    return row


def test_v1322_persistent_run_flags_are_signal_time_only_and_frozen():
    short = _risk_row(1, confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF, run_hours=35.9, ema_atr=2.0)
    long_stretched = _risk_row(2, confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF, run_hours=36.0, ema_atr=3.1)
    strict = _risk_row(3, confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF, run_hours=48.0, ema_atr=3.0)

    assert persistent_run_risk_flags(short) == (False, False)
    assert persistent_run_risk_flags(long_stretched) == (True, False)
    assert persistent_run_risk_flags(strict) == (True, True)


def test_v1322_calibration_is_censor_safe_and_forward_starts_at_new_freeze():
    cal_bad = _risk_row(
        10,
        confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF - timedelta(hours=1),
        run_hours=48.0,
        ema_atr=2.0,
        breach_hours=50,
    )
    cal_safe = _risk_row(
        11,
        confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF - timedelta(hours=2),
        run_hours=10.0,
        ema_atr=4.0,
    )
    # A younger pre-freeze signal can still join calibration if its adverse event
    # already resolved before the freeze; a young non-breach remains censored.
    young_resolved = _risk_row(
        12,
        confirmed_at=PERSISTENT_RUN_RISK_FREEZE_AT - timedelta(hours=4),
        run_hours=50.0,
        ema_atr=2.0,
        breach_hours=2,
    )
    young_censored = _risk_row(
        14,
        confirmed_at=PERSISTENT_RUN_RISK_FREEZE_AT - timedelta(hours=3),
        run_hours=50.0,
        ema_atr=2.0,
    )
    young_censored["path_last_at"] = young_censored["confirmed_at"] + timedelta(hours=2)
    forward = _risk_row(
        13,
        confirmed_at=PERSISTENT_RUN_RISK_FREEZE_AT + timedelta(hours=1),
        run_hours=50.0,
        ema_atr=2.5,
        breach_hours=4,
    )
    # Even though the path is not 120h complete, an already-observed -100 event is resolved.
    forward["path_last_at"] = forward["confirmed_at"] + timedelta(hours=5)

    report = build_research_analytics(
        [cal_bad, cal_safe, young_resolved, young_censored, forward],
        generated_at=PERSISTENT_RUN_RISK_FREEZE_AT + timedelta(hours=8),
    )
    risk = report.persistent_run_risk

    cal_strict = risk.bucket("calibration", "persistent_run_36h_ema3", True)
    cal_other = risk.bucket("calibration", "persistent_run_36h_ema3", False)
    fwd_strict = risk.bucket("prospective", "persistent_run_36h_ema3", True)

    assert cal_strict is not None and cal_strict.signals == 3
    assert cal_strict.evaluable_120h == 2 and cal_strict.adverse_100_breaches == 2
    assert cal_strict.tp5_before_adverse_100 == 2
    assert cal_other is not None and cal_other.signals == 1 and cal_other.adverse_100_breaches == 0
    assert fwd_strict is not None and fwd_strict.signals == 1
    assert fwd_strict.evaluable_120h == 1 and fwd_strict.adverse_100_breaches == 1


def test_v1322_dataset_exports_persistent_run_flags_and_true_forward_cohort():
    row = _risk_row(
        20,
        confirmed_at=PERSISTENT_RUN_RISK_FREEZE_AT + timedelta(minutes=1),
        run_hours=40.0,
        ema_atr=2.5,
    )
    text = research_signal_dataset_csv([row]).decode("utf-8")
    header, values = text.splitlines()[:2]
    assert "persistent_run_long_flag" in header
    assert "persistent_run_strict_flag" in header
    assert "persistent_run_risk_cohort" in header
    assert "prospective" in values


def test_v1322_notifier_surfaces_research_only_calibration_and_forward_tracker():
    cal_bad = _risk_row(
        30,
        confirmed_at=PERSISTENT_RUN_RISK_NONBREACH_MATURITY_CUTOFF - timedelta(hours=1),
        run_hours=48.0,
        ema_atr=2.0,
        breach_hours=50,
    )
    forward = _risk_row(
        31,
        confirmed_at=PERSISTENT_RUN_RISK_FREEZE_AT + timedelta(hours=1),
        run_hours=50.0,
        ema_atr=2.5,
    )
    forward["path_last_at"] = forward["confirmed_at"] + timedelta(hours=2)
    report = build_research_analytics(
        [cal_bad, forward], generated_at=PERSISTENT_RUN_RISK_FREEZE_AT + timedelta(hours=3)
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
    # Persistent-run research remains in the report object/exports but is intentionally
    # kept off the trader-facing Discord decision surface.
    assert report.persistent_run_risk.buckets
    assert "Persistent-run continuation risk" not in text
    assert "3-Strategy Validation" in text
    assert "Forward Validation" in text
    for embed in embeds:
        notifier._validate_discord_embed(embed)
