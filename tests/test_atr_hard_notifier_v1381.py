import asyncio
from datetime import UTC, datetime

from app.models import RunSignal
from app.notifier import DiscordNotifier
from app.strategy_ids import CURRENT_STRATEGY_ID


class _Response:
    status_code = 204
    text = ""
    def raise_for_status(self):
        return None


class _Client:
    def __init__(self):
        self.posts = []
    async def post(self, url, json=None, data=None, files=None):
        self.posts.append((url, json if json is not None else {"data": data, "files": files}))
        return _Response()
    async def aclose(self):
        return None


def features(atr: float):
    return {
        "risk_tier": "standard",
        "run_score": 4,
        "exhaustion_score": 4,
        "distance_above_ema20_atr_4h": 2.0,
        "previous_momentum_1h": -0.01,
        "cross_section_percentile": 0.95,
        "daily_close_above_ema20": False,
        "daily_ema20_slope": -0.01,
        "daily_momentum_3d": -0.02,
        "daily_distance_above_ema20_atr": 1.0,
        "hours_run_to_breakdown": 12.0,
        "lower_high_and_close": True,
        "structural_break_15m": True,
        "atr_15m": atr,
        "retest_close": 0.1,
    }


def signal(atr: float):
    return RunSignal(
        symbol="ATR_USDT",
        signaled_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        level="confirmed_short",
        score=8,
        features=features(atr),
        reasons=["test"],
        episode_id=1,
    )


def test_current_subscriber_hard_filters_low_atr_and_sends_high_atr():
    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        subscriber_signal_strategy=CURRENT_STRATEGY_ID,
    )
    fake = _Client()
    notifier._client = fake

    asyncio.run(notifier.send_signal(signal(0.0024)))  # 0.024 < 0.02461
    assert fake.posts == []

    asyncio.run(notifier.send_signal(signal(0.0030)))  # 0.030 > 0.02461
    assert len(fake.posts) == 1
