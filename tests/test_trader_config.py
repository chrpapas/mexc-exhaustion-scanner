import pytest

from app.trader_config import TraderSettings


def test_paper_cross_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for key in (
        "TRADING_MODE",
        "TRADER_CAPITAL_STRATEGY",
        "TRADER_POSITION_MATURITY",
        "MEXC_API_KEY",
        "MEXC_API_SECRET",
        "MEXC_LIVE_ORDER_API_ENABLED",
        "LIVE_TRADING_CONFIRM",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = TraderSettings.from_env()
    assert settings.trading_mode == "paper"
    assert settings.capital_strategy == "cross_20"
    assert settings.position_maturity == "profit_20"
    assert settings.profit_target_pct == 20.0
    assert settings.position_fraction == 0.20
    assert settings.liquidation_proxy_pct == 400.0


def test_live_mode_is_fail_closed_without_explicit_arm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("MEXC_API_KEY", "key")
    monkeypatch.setenv("MEXC_API_SECRET", "secret")
    monkeypatch.delenv("MEXC_LIVE_ORDER_API_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="fail-closed"):
        TraderSettings.from_env()

def test_fixed_maturity_config(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_POSITION_MATURITY", "7d")
    settings = TraderSettings.from_env()
    assert settings.position_maturity == "7d"
    assert settings.maturity_seconds == 7 * 24 * 60 * 60

