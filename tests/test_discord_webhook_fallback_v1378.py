from app.config import Settings


def _base(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for name in (
        "DISCORD_PERFORMANCE_WEBHOOK_URL",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_TRADER_EVENTS_WEBHOOK_URL",
        "DISCORD_TRADER_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_performance_webhook_falls_back_to_trader_events(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL", "https://example.invalid/trader-events")
    settings = Settings.from_env()
    assert settings.discord_performance_webhook_url == "https://example.invalid/trader-events"


def test_performance_webhook_fallback_priority(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("DISCORD_TRADER_WEBHOOK_URL", "https://example.invalid/legacy-trader")
    monkeypatch.setenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL", "https://example.invalid/trader-events")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/general")
    monkeypatch.setenv("DISCORD_PERFORMANCE_WEBHOOK_URL", "https://example.invalid/performance")
    settings = Settings.from_env()
    assert settings.discord_performance_webhook_url == "https://example.invalid/performance"


def test_performance_webhook_falls_back_to_legacy_trader(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("DISCORD_TRADER_WEBHOOK_URL", "https://example.invalid/legacy-trader")
    settings = Settings.from_env()
    assert settings.discord_performance_webhook_url == "https://example.invalid/legacy-trader"
