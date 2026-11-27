"""Guardian transient venue/ledger failure classification: fail closed."""
import asyncio

import pytest

import app.t100_v40_guardian_account_and_owned as mod
from app.t100_v40_canary_durable_guardian import _safe_observer_detail
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader


def _fixture():
    # No HTTP calls or SQL connections: exact production types, dummy pool.
    return (
        PostgresFourSlotLedgerReader(object()),
        object.__new__(GetOnlyMexcTransport),
    )


@pytest.mark.parametrize("failure_stage,code",[
    ("components","V40_GUARDIAN_READONLY_COMPONENTS_INIT_FAILED_HALT"),
    ("hedge_get","V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT"),
    ("snapshot","V40_GUARDIAN_SNAPSHOT_RUNTIME_EXCEPTION_HALT"),
])
def test_external_observation_failure_is_specific_and_still_critical(
    monkeypatch,failure_stage,code,
):
    ledger,signed=_fixture()
    def components(_):
        if failure_stage=="components":
            raise RuntimeError("SENSITIVE_PASSWORD_FROM_LIB")
        return (object(),object())
    class FakeReadOnlyProbe:
        def __init__(self,_):pass
        async def raw_position_mode(self):
            if failure_stage=="hedge_get":
                raise RuntimeError("SENSITIVE_MEXC_TOKEN")
            return 1
    class FakeCoordinator:
        def __init__(self,**kwargs):pass
        async def inspect(self):
            if failure_stage=="snapshot":
                raise RuntimeError("SENSITIVE_POSTGRES_DSN")
            raise AssertionError("not expected")
    monkeypatch.setattr(mod,"_components",components)
    monkeypatch.setattr(mod,"ReadOnlyMexcProbe",FakeReadOnlyProbe)
    monkeypatch.setattr(mod,"FourSlotSnapshotCoordinator",FakeCoordinator)
    hb=asyncio.run(mod.observe_v40_account_and_owned(
        ledger=ledger,signed_get=signed,
    ))
    assert hb.status==code
    assert _safe_observer_detail(hb)==code
    assert hb.incident_alert is True
    assert hb.monitored_account_flat is False
    assert hb.entry_allowed is False
    assert hb.live_authorization is False
    assert hb.mexc_posts==0 and hb.database_writes==0
    assert "SENSITIVE" not in repr(hb)
