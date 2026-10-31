"""GET/SELECT-only MEXC read integration for frozen T100 V40 four-slot gates.

Wires existing authenticated PRIVATE GET contract to the real
account-completeness probe, exact external order lookup, non-flat durable
recovery and staged protected-fill observation. Every output is observation,
never permission to submit orders, amend stops or acknowledge a fill.

No credentials loaded here. No MEXC client constructed. No HTTP POST, DB
mutation, migration, Render or notification side effects. Callers inject an
already configured GET-only client and an independent SELECT-only ledger.
"""
from __future__ import annotations

from typing import Any, Sequence

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_attempt_outcome import (
    ProtectedAttemptObservation, observe_staged_protected_attempts,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator, FourSlotSnapshotResult,
    LedgerSnapshotReader,
)
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_readonly_probe import PrivateGet, ReadOnlyMexcProbe


class FourSlotMexcReadVenue:
    """Only the account-wide GETs and exact order-origin GETs already reviewed."""

    def __init__(self, probe: ReadOnlyMexcProbe) -> None:
        if not isinstance(probe, ReadOnlyMexcProbe):
            raise TypeError("FOUR_SLOT_GET_PROBE_REQUIRED")
        self.probe = probe

    async def open_positions(self) -> list[dict[str, Any]]:
        return await self.probe.raw_open_positions()

    async def open_stop_orders(self) -> list[dict[str, Any]]:
        return await self.probe.raw_open_stops()

    async def order_by_external_oid(
        self, symbol: str, external_oid: str,
    ) -> dict[str, Any] | None:
        return await self.probe.raw_order_by_external_oid(
            symbol, external_oid,
        )

    async def order_deal_details(
        self, order_id: str,
    ) -> list[dict[str, Any]]:
        return await self.probe.raw_order_deal_details(order_id)


class FourSlotMexcCompleteness:
    """Actual GET-only inventory completeness, not a caller's asserted booleans."""

    def __init__(self, probe: ReadOnlyMexcProbe) -> None:
        if not isinstance(probe, ReadOnlyMexcProbe):
            raise TypeError("FOUR_SLOT_GET_PROBE_REQUIRED")
        self.probe = probe

    async def inspect(self) -> dict[str, Any]:
        result = await run_current_inventory_completeness_probe(self.probe)
        if not isinstance(result, dict):
            raise RuntimeError("FOUR_SLOT_COMPLETENESS_RESPONSE_INVALID")
        # Even a positive result is NONATOMIC, nonauthorizing venue evidence.
        if result.get("exchange_atomicity_verified") is not False:
            raise RuntimeError("FOUR_SLOT_GET_ATOMICITY_ASSERTION_INVALID")
        if result.get("entry_allowed") is not False:
            raise RuntimeError("FOUR_SLOT_GET_ENTRY_ASSERTION_INVALID")
        if result.get("live_authorization") is not False:
            raise RuntimeError("FOUR_SLOT_GET_AUTHORIZATION_ASSERTION_INVALID")
        return result


def _components(
    client: PrivateGet,
) -> tuple[FourSlotMexcReadVenue, FourSlotMexcCompleteness]:
    probe = ReadOnlyMexcProbe(client)
    return FourSlotMexcReadVenue(probe), FourSlotMexcCompleteness(probe)


async def inspect_existing_four_slots_get_only(
    *, ledger: LedgerSnapshotReader, client: PrivateGet,
    shared_manual_observation: bool = False,
) -> FourSlotSnapshotResult:
    """Observe all exposure; optional shared mode separates external positions.

    Legacy strict mode stays unchanged. Shared mode is GET-only and never
    supplies a permit to execute or close any position.
    """
    venue, completeness = _components(client)
    return await FourSlotSnapshotCoordinator(
        ledger=ledger, venue=venue, completeness=completeness,
    ).inspect(shared_manual_observation=shared_manual_observation)


async def inspect_staged_four_slot_fill_get_only(
    *, stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    client: PrivateGet,
) -> ProtectedAttemptObservation:
    """Observe a staged outcome; never retry an ambiguous exchange order."""
    venue, completeness = _components(client)
    return await observe_staged_protected_attempts(
        stages=stages, intents=intents,
        pre_attempt_witness=pre_attempt_witness,
        venue=venue, completeness=completeness,
    )
