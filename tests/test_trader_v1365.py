from app.trader_config import TraderSettings


def _clear(monkeypatch):
    for key in (
        "TRADER_EXECUTION_STRATEGY",
        "TRADER_CATASTROPHIC_STOP_PCT",
        "TRADER_MAX_OPEN_POSITIONS",
        "TRADER_SLOT_ALLOCATION_PCT",
        "TRADER_MAX_TOTAL_EXPOSURE_PCT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")


def test_v1365_promoted_default(monkeypatch):
    _clear(monkeypatch)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == "tp5_sl100_lae10_24_q1_daily_core_persistence_skip_v2"
    assert settings.catastrophic_stop_pct == 100
    assert settings.max_open_positions == 6
    assert abs(settings.slot_allocation_pct - (50.0 / 6.0)) < 1e-9
    assert settings.max_total_exposure_pct == 50
    assert settings.uses_lae10_24_q1
    assert settings.uses_catastrophic_stop
    assert settings.uses_daily_core_skip
    assert settings.uses_daily_bull_persistence_v2_skip


def test_legacy_sl75_v2_still_supported(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_daily_core_persistence_skip_v2")
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == "tp5_sl75_daily_core_persistence_skip_v2"
    assert settings.catastrophic_stop_pct == 75
    assert not settings.uses_lae10_24_q1
