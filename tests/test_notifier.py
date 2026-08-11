from app.notifier import DiscordNotifier


def test_default_discord_signal_levels_are_quiet():
    notifier = DiscordNotifier(None)
    assert not notifier.should_send_signal("run_watch")
    assert notifier.should_send_signal("exhaustion_watch")
    assert not notifier.should_send_signal("breakdown_watch")
    assert notifier.should_send_signal("confirmed_short")


def test_discord_signal_levels_are_configurable():
    notifier = DiscordNotifier(None, {"confirmed_short"})
    assert not notifier.should_send_signal("exhaustion_watch")
    assert notifier.should_send_signal("confirmed_short")

import asyncio
from datetime import UTC, date, datetime

from app.performance import HorizonSummary, PerformanceSummary


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
        hours=hours,
        matured_total=2,
        matured_today=1,
        win_rate=0.5,
        standard_total=1,
        standard_win_rate=1.0,
        standard_avg_return=value * 1.5,
        standard_sum_return=value * 1.5,
        high_risk_total=1,
        high_risk_win_rate=0.0,
        high_risk_avg_return=value * 0.5,
        high_risk_sum_return=value * 0.5,
        avg_return=value,
        sum_return=value * 2,
    )


def test_performance_report_renders_with_formatting_helpers():
    notifier = DiscordNotifier("https://discord.invalid/webhook")
    fake = _FakeClient()
    notifier._client = fake
    report = PerformanceSummary(
        report_date=date(2026, 8, 11),
        confirmed_today=1,
        open_count=2,
        open_avg_return=-0.0043,
        open_sum_return=-0.0086,
        horizon_24h=_horizon(24, 0.10),
        horizon_48h=_horizon(48, 0.15),
        horizon_72h=_horizon(72, 0.20),
        avg_return_1h=-0.01,
        avg_return_4h=0.02,
        avg_return_12h=0.03,
        avg_mfe_72h=0.25,
        avg_mae_72h=-0.30,
        best_symbol_72h="BEST_USDT",
        best_return_72h=0.50,
        worst_symbol_72h="WORST_USDT",
        worst_return_72h=-0.20,
    )

    sent = asyncio.run(
        notifier.send_performance_report(
            report,
            label="ON-DEMAND SHADOW PERFORMANCE",
            as_of=datetime(2026, 8, 11, 6, 52, tzinfo=UTC),
            timezone_name="Europe/Zurich",
        )
    )

    assert sent is True
    content = fake.posts[0][1]["content"]
    assert "Open mark-to-market: -0.43% avg" in content
    assert "24h: 2 matured" in content
    assert "48h: 2 matured" in content
    assert "72h: 2 matured" in content
    assert "24h STANDARD — n=1 | win 100.00% | avg 15.00% | sum 15.00%" in content
    assert "24h HIGH+EXTREME — n=1 | win 0.00% | avg 5.00% | sum 5.00%" in content
    assert "Best 72h: BEST_USDT 50.00%" in content
