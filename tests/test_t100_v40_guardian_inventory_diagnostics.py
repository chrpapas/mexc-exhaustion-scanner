"""V40 guardian inventory GET diagnostics: fixed categories, same sticky safety halt.

No live credentials, network, DB mutation, latch change, or POST.
"""
from __future__ import annotations

import httpx
import pytest

from app.t100_live_guardian_readonly_once import (
    GetOnlyMexcTransport, GuardianOnceHalted,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator, FourSlotSnapshotResult,
)
from app.t100_v40_guardian_get_failure_codes import (
    safe_inventory_exception_category,
    guardian_inventory_diagnostic_halt,
    SAFE_GUARDIAN_INVENTORY_DIAGNOSTIC_HALTS,
)
from app.t100_v40_canary_durable_guardian import (
    _safe_observer_detail, _status_for_outbox,
)
from app.t100_v40_guardian_account_and_owned import (
    observe_v40_account_and_owned,
)
from app.t100_v40_guardian_ledger_reader import PostgresV40GuardianLedgerReader


class Client:
    async def get(self, path, **kwargs):
        return httpx.Response(
            429, json={"message": "SENSITIVE USER DATA"},
            request=httpx.Request("GET", "https://api.mexc.com" + path),
        )


@pytest.mark.asyncio
async def test_opt_in_inventory_http_reason_is_finite_and_redacted():
    tr=GetOnlyMexcTransport(
        Client(), api_key="a"*16, api_secret="b"*32,
        diagnostic_get_errors=True,
    )
    with pytest.raises(GuardianOnceHalted) as caught:
        await tr._private_get("/api/v1/private/stoporder/open_orders")
    assert str(caught.value) == "GUARDIAN_GET_HTTP_429"
    assert "SENSITIVE" not in str(caught.value)
    with pytest.raises(VenueReadHalted) as nested:
        await ReadOnlyMexcProbe(tr).raw_open_stops()
    assert safe_inventory_exception_category(nested.value) == "HTTP_429"


@pytest.mark.asyncio
async def test_without_opt_in_previous_generic_error_remains():
    tr=GetOnlyMexcTransport(
        Client(), api_key="a"*16, api_secret="b"*32,
    )
    with pytest.raises(GuardianOnceHalted, match="MEXC_SIGNED_GET_TRANSPORT_FAILED"):
        await tr._private_get("/api/v1/private/stoporder/open_orders")


@pytest.mark.asyncio
async def test_complete_inventory_failure_preserves_bounded_stage_and_category():
    class Probe:
        async def raw_open_positions(self):
            return []
        async def raw_open_stops(self):
            try:
                raise GuardianOnceHalted("GUARDIAN_GET_HTTP_429")
            except GuardianOnceHalted as error:
                raise VenueReadHalted("OPEN_STOPS_GET_FAILED") from error
    result=await run_current_inventory_completeness_probe(Probe())
    assert result["status"] == "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED"
    assert result["failure_stage"] == "BEFORE_STOPS"
    assert result["failure_category"] == "HTTP_429"
    assert result["entry_allowed"] is False
    assert result["live_authorization"] is False


@pytest.mark.asyncio
async def test_snapshot_first_read_failure_is_bounded(monkeypatch):
    from app import t100_live_four_slot_snapshot_coordinator as module
    monkeypatch.setattr(module, "_ledger_digest", lambda ledger: "digest")
    class Ledger:
        async def read_snapshot(self):
            return object()
    class Venue:
        async def open_positions(self):
            raise VenueReadHalted("POSITIONS_RESPONSE_UNVERIFIED")
    class Completeness:
        async def inspect(self):
            raise AssertionError("must not run")
    snap=await FourSlotSnapshotCoordinator(
        ledger=Ledger(), venue=Venue(), completeness=Completeness(),
    ).inspect()
    assert snap.status == "INVENTORY_READ_OR_FORMAT_FAILED"
    assert snap.mismatch_diagnostic == {
        "stage": "FIRST_POSITIONS", "category": "SCHEMA",
    }


@pytest.mark.asyncio
async def test_guardian_reports_category_but_preserves_original_critical_gate(monkeypatch):
    from app import t100_v40_guardian_account_and_owned as module
    class Coordinator:
        def __init__(self, **kwargs):
            pass
        async def inspect(self):
            return FourSlotSnapshotResult(
                "INVENTORY_COMPLETENESS_UNVERIFIED",
                inventory={
                    "status": "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED",
                    "failure_stage": "BEFORE_STOPS",
                    "failure_category": "HTTP_429",
                },
            )
    async def hedge_mode(*args, **kwargs):
        return 1
    monkeypatch.setattr(module, "FourSlotSnapshotCoordinator", Coordinator)
    monkeypatch.setattr(module, "_hedge_mode_bounded_get", hedge_mode)
    monkeypatch.setattr(module, "_components", lambda signed_get: (object(), object()))
    ledger=PostgresV40GuardianLedgerReader.__new__(PostgresV40GuardianLedgerReader)
    tr=GetOnlyMexcTransport(Client(), api_key="a"*16, api_secret="b"*32)
    heartbeat=await observe_v40_account_and_owned(
        ledger=ledger, signed_get=tr,
    )
    expected="V40_GUARDIAN_INVENTORY_BEFORE_STOPS_HTTP_429_HALT"
    assert heartbeat.status == expected
    assert expected in SAFE_GUARDIAN_INVENTORY_DIAGNOSTIC_HALTS
    assert _safe_observer_detail(heartbeat) == expected
    assert _status_for_outbox(heartbeat) == (
        "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
    )
    assert heartbeat.monitored_account_flat is False
    assert heartbeat.live_authorization is False


def test_untrusted_status_never_leaks_into_heartbeat():
    assert guardian_inventory_diagnostic_halt(
        "BEFORE_STOPS", "http://private/token"
    ) is None
    assert guardian_inventory_diagnostic_halt(
        "private_account", "HTTP_429"
    ) is None
    assert safe_inventory_exception_category(
        ValueError("PRIVATE_URL_AND_TOKEN")
    ) == "UNCLASSIFIED"
