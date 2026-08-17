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
    assert len(fake.posts) == 4
    assert all(url == "https://discord.invalid/stats" for url, _ in fake.posts)
    assert all(payload["username"] == "Exhaustion Scanner • Stats" for _, payload in fake.posts)
    assert all(len(payload["embeds"]) == 1 for _, payload in fake.posts)

    embeds = [payload["embeds"][0] for _, payload in fake.posts]
    all_text = "\n".join(
        [embed.get("title", "") + "\n" + embed.get("description", "")
         + "\n" + "\n".join(field["name"] + " " + field["value"] for field in embed.get("fields", []))
         for embed in embeds]
    )
    assert "Performance Board" in all_text
    assert "STANDARD Signal Outcomes" in all_text
    assert "HIGH RISK Signal Outcomes" in all_text
    assert "EXTREME RISK" not in all_text
    assert "-100% adverse" in all_text
    assert "-200% adverse" in all_text
    assert "-300% adverse" in all_text
    assert "-400% adverse" in all_text
    assert "20%-sized acct equiv" not in all_text
    assert "+20% before breach" not in all_text
    assert "+20% Target Race • Horizon Independent" in all_text
    assert "1D Outcomes" in all_text
    assert "Not profitable" in all_text
    assert "Adverse crossed before horizon" in all_text
    assert "avg t" in all_text
    assert "Avg raw" in all_text
    assert "Σ raw" in all_text
    assert "How to Read the Signal Outcomes" in all_text


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
    assert len(fake.posts) == 4
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
