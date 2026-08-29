import asyncio
from datetime import UTC, date, datetime

from app.notifier import DiscordNotifier
from app.performance import (
    HorizonSummary,
    HorizonSurvivalSummary,
    PerformanceSummary,
    ProfitTargetModelSummary,
    ProfitTargetSummary,
    StrategyMatrixSummary,
    StrategyRowSummary,
    StrategyThresholdSummary,
    SurvivalModelSummary,
    WeeklyRiskSummary,
    TP5PublicSummary,
    HighRiskTp20PublicSummary,
    Standard7dPublicSummary,
    Normalized7dStrategySummary,
    AccountRunRateSummary,
)


def test_default_discord_signal_levels_are_quiet():
    notifier = DiscordNotifier(None)
    assert not notifier.should_send_signal("run_watch")
    assert not notifier.should_send_signal("exhaustion_watch")
    assert not notifier.should_send_signal("breakdown_watch")
    assert notifier.should_send_signal("confirmed_short")


def test_discord_signal_levels_are_hard_gated_to_confirmed_short():
    notifier = DiscordNotifier(None, {"exhaustion_watch", "confirmed_short"})
    assert not notifier.should_send_signal("run_watch")
    assert not notifier.should_send_signal("exhaustion_watch")
    assert not notifier.should_send_signal("breakdown_watch")
    assert notifier.should_send_signal("confirmed_short")


class _FakeResponse:
    status_code = 204
    text = ""

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.posts = []

    async def post(self, url, json=None, data=None, files=None):
        payload = json if json is not None else {"data": data, "files": files}
        self.posts.append((url, payload))
        return _FakeResponse()

    async def aclose(self):
        return None


def test_extreme_risk_confirmed_short_is_never_sent_to_discord():
    from app.models import RunSignal

    notifier = DiscordNotifier("https://discord.invalid/signals")
    fake = _FakeClient()
    notifier._client = fake
    signal = RunSignal(
        symbol="EXTREME_USDT",
        signaled_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        level="confirmed_short",
        score=10,
        features={"risk_tier": "extreme_risk"},
        reasons=["synthetic"],
        episode_id=1,
    )
    asyncio.run(notifier.send_signal(signal))
    assert fake.posts == []


def _horizon(hours: int, value: float) -> HorizonSummary:
    return HorizonSummary(
        hours, 2, 1, 0.5,
        1, 1.0, value * 1.5, value * 1.5,
        1, 0.0, value * 0.5, value * 0.5,
        value, value * 2,
    )


def _survive(hours: int, label: str) -> HorizonSurvivalSummary:
    isolated = SurvivalModelSummary(1, 0.5, 1.0, 0.12, 0.12)
    cross = SurvivalModelSummary(2, 1.0, 0.5, 0.10, 0.20)
    return HorizonSurvivalSummary(hours, label, 2, isolated, cross)


def _weekly(label: str) -> WeeklyRiskSummary:
    return WeeklyRiskSummary(label, 2, 1.0, 0.5, 0.0, 0.5, 0.0)


def _target(label: str) -> ProfitTargetSummary:
    return ProfitTargetSummary(
        label,
        ProfitTargetModelSummary(10, 8, 7, 1, 2, 0.875, 30.0),
        ProfitTargetModelSummary(10, 7, 7, 0, 3, 1.0, 34.0),
    )




def _matrix(label: str) -> StrategyMatrixSummary:
    thresholds = tuple(
        StrategyThresholdSummary(
            adverse_limit_pct=limit, total=10, resolved=10, wins=10, failures=0, pending=0,
            win_rate=1.0, avg_profit=0.20, sum_profit=2.0, avg_time_to_target_hours=30.0
        )
        for limit in (100, 200, 300, 400)
    )
    rows = (
        StrategyRowSummary("profit_20", "+20% target", None, thresholds),
        StrategyRowSummary("24h", "1D profitable", 24, thresholds),
        StrategyRowSummary("48h", "2D profitable", 48, thresholds),
        StrategyRowSummary("72h", "3D profitable", 72, thresholds),
        StrategyRowSummary("168h", "7D profitable", 168, thresholds),
    )
    return StrategyMatrixSummary(label, 10, rows)

def _report() -> PerformanceSummary:
    return PerformanceSummary(
        report_date=date(2026, 8, 12),
        confirmed_today=1,
        open_count=2,
        open_avg_return=-0.0043,
        open_sum_return=-0.0086,
        horizon_24h=_horizon(24, 0.10),
        horizon_48h=_horizon(48, 0.15),
        horizon_72h=_horizon(72, 0.20),
        horizon_168h=_horizon(168, 0.25),
        standard_survival=tuple(_survive(h, "STANDARD") for h in (24, 48, 72, 168)),
        risky_survival=tuple(_survive(h, "HIGH+EXTREME") for h in (24, 48, 72, 168)),
        standard_weekly=_weekly("STANDARD"),
        risky_weekly=_weekly("HIGH+EXTREME"),
        high_weekly=_weekly("HIGH RISK"),
        extreme_weekly=_weekly("EXTREME RISK"),
        standard_profit_target=_target("STANDARD"),
        risky_profit_target=_target("HIGH+EXTREME"),
        standard_strategy_matrix=_matrix("STANDARD"),
        risky_strategy_matrix=_matrix("HIGH+EXTREME"),
        high_strategy_matrix=_matrix("HIGH RISK"),
        extreme_strategy_matrix=_matrix("EXTREME RISK"),
        avg_return_1h=-0.01,
        avg_return_4h=0.02,
        avg_return_12h=0.03,
        avg_mfe_7d=0.40,
        avg_mae_7d=-0.50,
        best_symbol_7d="BEST_USDT",
        best_return_7d=0.60,
        worst_symbol_7d="WORST_USDT",
        worst_return_7d=-0.30,
        tp5_public=TP5PublicSummary(12, 12, 1.0, 2.0, 6.0, 0.07, 0, 0.05, 0.60),
        high_risk_tp20_public=HighRiskTp20PublicSummary(27, 12, 12/27, 18, 9, 18/27, 0.0635, 0.08, 1.7145, 0.20, -0.4669, 65.76),
        standard_7d_public=Standard7dPublicSummary(5, 0.8, 0.30, 0.28, 4, 1, 1.50, 0.70, -0.20),
        comparison_start_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        comparison_end_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        tp5_7d_comparison=Normalized7dStrategySummary(
            sample=12, target_hits=12, unresolved_at_7d=0, wins=12, losses=0, positive_rate=1.0,
            avg_return=0.05, median_return=0.05, sum_return=0.60, best_return=0.05, worst_return=0.05,
            avg_effective_holding_hours=4.0, median_effective_holding_hours=2.0,
            breach_50=1, breach_100=0, breach_200=0, breach_300=0, worst_adverse=0.31,
        ),
        tp20_7d_comparison=Normalized7dStrategySummary(
            sample=6, target_hits=3, unresolved_at_7d=3, wins=5, losses=1, positive_rate=5/6,
            avg_return=0.12, median_return=0.15, sum_return=0.72, best_return=0.20, worst_return=-0.10,
            avg_effective_holding_hours=96.0, median_effective_holding_hours=120.0,
            breach_50=2, breach_100=1, breach_200=0, breach_300=0, worst_adverse=1.25,
        ),
        standard_7d_comparison=Normalized7dStrategySummary(
            sample=5, target_hits=0, unresolved_at_7d=0, wins=4, losses=1, positive_rate=0.8,
            avg_return=0.30, median_return=0.28, sum_return=1.50, best_return=0.70, worst_return=-0.20,
            avg_effective_holding_hours=168.0, median_effective_holding_hours=168.0,
            breach_50=1, breach_100=0, breach_200=0, breach_300=0, worst_adverse=0.65,
        ),
        tp5_account_run_rate=AccountRunRateSummary(
            "tp5", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 12, tzinfo=UTC),
            11.0, 20, 18, 16, 2, 0, 1, 1, 0.084, 0.2291, 2291.0, 0.12, 0.30, 26.4, 0.041, 0.084 / 0.041,
        ),
        tp20_account_run_rate=AccountRunRateSummary(
            "tp20", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 12, tzinfo=UTC),
            11.0, 10, 8, 5, 3, 0, 2, 0, 0.031, 0.0845, 845.0, 0.07, 0.10, 38.5, 0.12, 0.031 / 0.12,
        ),
        standard_7d_account_run_rate=AccountRunRateSummary(
            "standard_7d", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 12, tzinfo=UTC),
            11.0, 10, 6, 3, 3, 0, 4, 0, 0.102, 0.2782, 2782.0, 0.14, 0.15, 51.3, 0.055, 0.102 / 0.055,
        ),
    )


def test_performance_report_uses_dedicated_stats_webhook_and_embeds():
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake

    sent = asyncio.run(
        notifier.send_performance_report(
            _report(),
            label="ON-DEMAND SHADOW PERFORMANCE",
            as_of=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            timezone_name="Europe/Zurich",
        )
    )

    assert sent
    assert len(fake.posts) == 1
    assert fake.posts[0][0] == "https://discord.invalid/stats"
    payload = fake.posts[0][1]
    assert payload["username"] == "Exhaustion Scanner • Stats"
    assert len(payload["embeds"]) == 1

    embed = payload["embeds"][0]
    all_text = embed.get("title", "") + "\n" + embed.get("description", "") + "\n" + "\n".join(
        field["name"] + " " + field["value"] for field in embed.get("fields", [])
    )
    assert "Performance & Playbook" in all_text
    assert "Suggested execution" in all_text
    assert "TP5 indefinite" in all_text
    assert "TP5 + SL75" in all_text
    assert "7D hold" in all_text
    assert "max **6** open positions" in all_text
    assert "30%" in all_text
    assert "0.08% fee per fill" in all_text
    assert "TP20" not in all_text
    assert "EntryGate" not in all_text


def test_performance_webhook_falls_back_to_signal_webhook_for_backward_compatibility():
    notifier = DiscordNotifier("https://discord.invalid/signals")
    fake = _FakeClient()
    notifier._client = fake
    sent = asyncio.run(notifier.send_performance_report(_report()))
    assert sent
    assert fake.posts[0][0] == "https://discord.invalid/signals"


def test_performance_cards_respect_discord_embed_limits():
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake

    sent = asyncio.run(notifier.send_performance_report(_report()))

    assert sent
    assert len(fake.posts) == 1
    for _, payload in fake.posts:
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert notifier._discord_embed_char_count(embed) <= 6000
        assert len(embed.get("fields", [])) <= 25
        for field in embed.get("fields", []):
            assert len(field.get("name", "")) <= 256
            assert len(field.get("value", "")) <= 1024



def test_signal_ledger_report_sends_compact_table_images_and_attaches_csv():
    from app.signal_ledger import build_signal_ledger, signal_ledger_csv
    from app.signal_ledger_table import render_signal_ledger_tables
    from datetime import timedelta

    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    rows = []
    for index in range(16):
        risk = "standard" if index < 8 else ("high_risk" if index < 12 else "extreme_risk")
        rows.append({
            "episode_id": index + 1,
            "symbol": f"TOKEN{index}_USDT",
            "confirmed_at": confirmed + timedelta(hours=index),
            "entry_price": 1.0,
            "risk_tier": risk,
            "current_return_pct": 0.05 if index % 2 == 0 else -0.05,
            "first_profit_at": confirmed + timedelta(hours=index + 2),
            "target_20_at": confirmed + timedelta(hours=index + 30) if index % 3 == 0 else None,
            "isolated_100_breach_at": None,
            "adverse_200_breach_at": None,
            "adverse_300_breach_at": None,
            "cross_400_breach_at": None,
            "return_24h_pct": 0.10,
            "return_48h_pct": 0.20,
            "return_72h_pct": None,
            "return_168h_pct": None,
        })
    ledger = build_signal_ledger(rows, generated_at=confirmed + timedelta(days=5))
    table_images = render_signal_ledger_tables(ledger, timezone_name="Europe/Zurich")

    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        performance_webhook_url="https://discord.invalid/stats",
    )
    fake = _FakeClient()
    notifier._client = fake
    sent = asyncio.run(notifier.send_signal_ledger(
        ledger,
        csv_bytes=signal_ledger_csv(ledger),
        table_images=table_images,
        as_of=confirmed + timedelta(days=5),
        timezone_name="Europe/Zurich",
    ))

    assert sent
    assert len(fake.posts) == 1 + len(table_images)
    assert fake.posts[0][1]["files"] is not None
    assert "signal-outcome-ledger" in fake.posts[0][1]["files"]["files[0]"][0]
    for _, payload in fake.posts[1:]:
        assert payload["files"] is not None
        name, data, mime = payload["files"]["files[0]"]
        assert name.endswith(".png")
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert mime == "image/png"
        assert "attachment://" in payload["data"]["payload_json"]
