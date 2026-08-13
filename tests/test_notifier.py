import asyncio
from datetime import UTC, date, datetime

from app.notifier import DiscordNotifier
from app.performance import (
    HorizonSummary,
    HorizonSurvivalSummary,
    PerformanceSummary,
    ProfitTargetModelSummary,
    ProfitTargetSummary,
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
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.posts = []

    async def post(self, url, json):
        self.posts.append((url, json))
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
        standard_profit_target=_target("STANDARD"),
        risky_profit_target=_target("HIGH+EXTREME"),
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
    assert len(fake.posts) == 1
    url, payload = fake.posts[0]
    assert url == "https://discord.invalid/stats"
    assert payload["username"] == "Exhaustion Scanner • Stats"
    assert len(payload["embeds"]) == 4

    all_text = "\n".join(
        [embed.get("title", "") + "\n" + embed.get("description", "")
         + "\n" + "\n".join(field["name"] + " " + field["value"] for field in embed.get("fields", []))
         for embed in payload["embeds"]]
    )
    assert "Performance Board" in all_text
    assert "STANDARD Execution Risk" in all_text
    assert "HIGH + EXTREME Execution Risk" in all_text
    assert "1× isolated" in all_text
    assert "5× cross buffer" in all_text
    assert "20%-sized acct equiv" not in all_text
    assert "+20% before breach" not in all_text
    assert "+20% Profit Target • Horizon Independent" in all_text
    assert "+20% before -100% short loss" in all_text
    assert "+20% before -400% short loss" in all_text
    assert "Avg time to +20%" in all_text
    assert "Trader Strategy" in all_text


def test_performance_webhook_falls_back_to_signal_webhook_for_backward_compatibility():
    notifier = DiscordNotifier("https://discord.invalid/signals")
    fake = _FakeClient()
    notifier._client = fake
    sent = asyncio.run(notifier.send_performance_report(_report()))
    assert sent
    assert fake.posts[0][0] == "https://discord.invalid/signals"
