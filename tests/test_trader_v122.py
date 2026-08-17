import pytest


@pytest.mark.asyncio
async def test_periodic_heartbeat_does_not_send_discord(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader
    from app.trader_config import TraderSettings

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    notices = []

    async def fake_notify(*args, **kwargs):
        notices.append((args, kwargs))

    monkeypatch.setattr(trader, "_notify", fake_notify)
    trader._last_discord_heartbeat = 0.0
    await trader._maybe_heartbeat()
    assert notices == []
