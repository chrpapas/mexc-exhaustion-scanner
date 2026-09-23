from __future__ import annotations

from app.config import Settings


def test_snapshot_audit_defaults_off(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/test")
    monkeypatch.delenv("SNAPSHOT_AUDIT_ENABLED", raising=False)
    monkeypatch.delenv("SNAPSHOT_AUDIT_SYMBOLS", raising=False)
    monkeypatch.delenv("SNAPSHOT_AUDIT_STATES", raising=False)

    settings = Settings.from_env()

    assert settings.snapshot_audit_enabled is False
    assert settings.snapshot_audit_symbols == frozenset()
    assert settings.snapshot_audit_states == frozenset({"breakdown_watch"})


def test_snapshot_audit_env_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/test")
    monkeypatch.setenv("SNAPSHOT_AUDIT_ENABLED", "true")
    monkeypatch.setenv("SNAPSHOT_AUDIT_SYMBOLS", "apt_usdt, light_usdt")
    monkeypatch.setenv("SNAPSHOT_AUDIT_STATES", "breakdown_watch, exhaustion_watch")

    settings = Settings.from_env()

    assert settings.snapshot_audit_enabled is True
    assert settings.snapshot_audit_symbols == frozenset({"APT_USDT", "LIGHT_USDT"})
    assert settings.snapshot_audit_states == frozenset(
        {"breakdown_watch", "exhaustion_watch"}
    )
