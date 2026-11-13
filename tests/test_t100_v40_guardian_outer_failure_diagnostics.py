"""Nonauthorizing failure-phase diagnostics for the independent V40 guardian.

Offline only: no exchange or database network calls, no real credentials.
"""
import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

import app.t100_v40_guardian_account_and_owned as mod
from app.t100_v40_canary_durable_guardian import _safe_observer_detail


@pytest.mark.parametrize("failure_phase, expected_code", [
    ("ledger", "V40_GUARDIAN_LEDGER_POOL_CONNECT_FAILED_HALT"),
    ("observer", "V40_GUARDIAN_OBSERVATION_RUNTIME_FAILED_HALT"),
])
def test_outer_failure_is_specific_redacted_and_still_fails_closed(
    monkeypatch, failure_phase, expected_code,
):
    monkeypatch.setattr(mod, "verify_guardian_environment", lambda env: None)
    closed = []

    class FakePool:
        async def close(self):
            closed.append(True)

    async def fake_create_pool(**kwargs):
        if failure_phase == "ledger":
            raise RuntimeError("SENSITIVE_LEDGER_PASSWORD")
        return FakePool()

    class BrokenHttpClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("SENSITIVE_MEXC_API_KEY")

    monkeypatch.setitem(
        sys.modules, "asyncpg", SimpleNamespace(create_pool=fake_create_pool)
    )
    monkeypatch.setattr(httpx, "AsyncClient", BrokenHttpClient)
    env = {
        "T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB": "true",
        "T100_V40_GUARDIAN_SHARED_DB_DSN":
            "postgresql://guardian:SENSITIVE_PASSWORD@db/ledger",
        "T100_V40_SIGNAL_READER_DSN":
            "postgresql://readonly:different@db/signals",
    }

    hb = asyncio.run(mod.observe_v40_account_and_owned_network(env))
    assert hb.status == expected_code
    assert _safe_observer_detail(hb) == expected_code
    assert hb.incident_alert is True
    assert hb.monitored_account_flat is False
    assert hb.entry_allowed is False
    assert hb.live_authorization is False
    assert hb.mexc_posts == 0 and hb.database_writes == 0
    assert "SENSITIVE" not in repr(hb)
    assert closed == ([True] if failure_phase == "observer" else [])
