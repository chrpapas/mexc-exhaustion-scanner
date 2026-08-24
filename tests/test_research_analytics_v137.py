from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import (
    RESEARCH_OOS_FREEZE_AT,
    build_research_analytics,
    research_token_regime_csv,
)
from app.token_regime import (
    EPISODIC_CLASS,
    REGIME_FOLLOWER_CLASS,
    REGIME_MIN_PAIRED_RETURNS,
    build_token_regime_research,
)
from tests.test_research_analytics_v128 import _base_row


def _history_rows(episode_id: int, symbol: str, confirmed_at: datetime, kind: str, bars: int = 220):
    token = 1.0
    btc = 100.0
    start = confirmed_at - timedelta(hours=4 * (bars + 1))
    rows = []
    for i in range(bars + 1):
        if i:
            btc_ret = 0.004 if i % 6 in (0, 1, 2) else -0.003
            btc *= 1.0 + btc_ret
            if kind == "follower":
                token_ret = 1.15 * btc_ret + (0.0002 if i % 2 else -0.0002)
            elif kind == "episodic":
                token_ret = 0.0005 if i % 2 else -0.0004
                if i in (40, 85, 130, 175, 210):
                    token_ret = 0.12
            else:  # mixed
                token_ret = 0.45 * btc_ret + (0.001 if i % 5 == 0 else -0.0002)
                if i in (75, 165):
                    token_ret += 0.035
            token *= 1.0 + token_ret
        rows.append({
            "episode_id": episode_id,
            "symbol": symbol,
            "confirmed_at": confirmed_at,
            "open_time": start + timedelta(hours=4 * i),
            "token_close": token,
            "btc_close": btc,
        })
    return rows


def _six_signal_fixture():
    rows = []
    history = []
    kinds = ["follower", "follower", "mixed", "mixed", "episodic", "episodic"]
    for idx, kind in enumerate(kinds, start=1):
        row = _base_row(idx)
        row["confirmed_at"] = datetime(2026, 8, 10, tzinfo=UTC) + timedelta(hours=idx)
        row["symbol"] = f"{kind.upper()}{idx}_USDT"
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=idx)
        row["path_mae_before_target_5"] = -0.02 * idx
        rows.append(row)
        history.extend(_history_rows(idx, row["symbol"], row["confirmed_at"], kind))
    return rows, history


def test_v137_behavior_classifier_separates_steady_followers_from_spiky_tokens():
    rows, history = _six_signal_fixture()
    summary = build_token_regime_research(
        rows, history, discovery_cutoff=RESEARCH_OOS_FREEZE_AT
    )
    by_id = {item.episode_id: item for item in summary.profiles}

    assert summary.profiled_signals == 6
    assert summary.discovery_profiled_signals == 6
    assert all(item.paired_returns >= REGIME_MIN_PAIRED_RETURNS for item in by_id.values())
    assert by_id[1].behavior_class == REGIME_FOLLOWER_CLASS
    assert by_id[2].behavior_class == REGIME_FOLLOWER_CLASS
    assert by_id[5].behavior_class == EPISODIC_CLASS
    assert by_id[6].behavior_class == EPISODIC_CLASS
    assert by_id[1].market_r2 > by_id[5].market_r2
    assert by_id[5].positive_spike_concentration > by_id[1].positive_spike_concentration
    assert by_id[5].isolated_pump_rate > by_id[1].isolated_pump_rate


def test_v137_research_report_replays_behavior_filters_without_changing_baseline_tp5():
    rows, history = _six_signal_fixture()
    generated = datetime(2026, 8, 20, tzinfo=UTC)
    baseline = build_research_analytics(rows, generated_at=generated)
    report = build_research_analytics(
        rows, generated_at=generated, regime_history_rows=history
    )

    # Frozen TP5-All remains bit-for-bit the same research portfolio.
    assert report.calendar_throughput.tp5 == baseline.calendar_throughput.tp5

    variants = {item.strategy: item for item in report.regime_portfolios}
    no_regime = variants["tp5_no_regime_followers"]
    episodic_only = variants["tp5_episodic_only"]
    priority = variants["tp5_episodic_priority_same_bar"]

    assert no_regime.filtered_strategy == 2
    assert no_regime.entered == 4
    assert episodic_only.filtered_strategy == 4
    assert episodic_only.entered == 2
    assert priority.filtered_strategy == 0
    assert priority.entered == report.calendar_throughput.tp5.entered

    csv_text = research_token_regime_csv(report).decode("utf-8")
    assert "behavior_class" in csv_text
    assert "REGIME_FOLLOWER" in csv_text
    assert "EPISODIC" in csv_text


def test_v137_insufficient_history_is_not_rejected_by_conservative_filter():
    rows = [_base_row(1), _base_row(2)]
    for row in rows:
        row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
    report = build_research_analytics(
        rows, generated_at=datetime(2026, 8, 20, tzinfo=UTC), regime_history_rows=[]
    )
    variants = {item.strategy: item for item in report.regime_portfolios}
    conservative = variants["tp5_no_regime_followers"]
    assert report.token_regime.insufficient_signals == 2
    assert conservative.filtered_strategy == 0
    assert conservative.entered == report.calendar_throughput.tp5.entered


def test_v137_notifier_adds_token_behavior_card_and_regime_csv_attachment():
    import asyncio

    from app.notifier import DiscordNotifier
    from tests.test_notifier import _FakeClient

    rows, history = _six_signal_fixture()
    report = build_research_analytics(
        rows,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        regime_history_rows=history,
    )
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    regime_csv = research_token_regime_csv(report)

    assert asyncio.run(notifier.send_research_analytics(report, regime_csv=regime_csv))
    titles = []
    attachment_names = []
    for _, payload in fake.posts:
        if "data" in payload:
            data = payload.get("data") or {}
            import json
            raw = data.get("payload_json")
            if raw:
                body = json.loads(raw)
                titles.extend(embed.get("title") for embed in body.get("embeds", []))
            files = payload.get("files") or {}
            attachment_names.extend(value[0] for value in files.values())
        else:
            titles.extend(embed.get("title") for embed in payload.get("embeds", []))
    assert "🧬 Token Behaviour • Regime Dependency" not in titles
    assert "🔬 Exhaustion Scanner • Strategy Validation" in titles
    assert not any(name.startswith("research-token-regime-") for name in attachment_names)


def test_v139_token_behavior_card_reports_capital_time_efficiency_metrics():
    import asyncio
    import json

    from app.notifier import DiscordNotifier
    from tests.test_notifier import _FakeClient

    rows, history = _six_signal_fixture()
    generated_at = datetime(2026, 8, 20, tzinfo=UTC)
    report = build_research_analytics(
        rows,
        generated_at=generated_at,
        regime_history_rows=history,
    )
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake

    assert asyncio.run(notifier.send_research_analytics(report))
    token_behavior_text = ""
    for _, payload in fake.posts:
        bodies = []
        if "data" in payload:
            raw = (payload.get("data") or {}).get("payload_json")
            if raw:
                bodies.append(json.loads(raw))
        else:
            bodies.append(payload)
        for body in bodies:
            for embed in body.get("embeds", []):
                if embed.get("title") == "🧬 Token Behaviour • Regime Dependency":
                    token_behavior_text = "\n".join(
                        field.get("value", "") for field in embed.get("fields", [])
                    )

    assert token_behavior_text == ""
    published_text = "\n".join(
        embed.get("title", "") + " " + " ".join(field.get("name", "") + " " + field.get("value", "") for field in embed.get("fields", []))
        for _, payload in fake.posts
        for body in ([json.loads((payload.get("data") or {}).get("payload_json"))] if "data" in payload and (payload.get("data") or {}).get("payload_json") else [payload])
        for embed in body.get("embeds", [])
    )
    assert "TP5 Frequent" in published_text
    assert "slot-days **" not in published_text
    assert "idle capacity **" not in published_text
