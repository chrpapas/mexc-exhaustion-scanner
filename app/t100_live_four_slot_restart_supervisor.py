"""Nonauthorizing single-cycle V40 restart supervisor (read-only only).

First uses a restricted PostgreSQL REPEATABLE READ restart triage. If any
durable order attempt is unresolved, it STOPS before exchange GETs, because
normal inventory observation must not be mistaken for permission to retry
or adopt a manual position. If no attempt is unresolved, it MAY run the
existing signed-GET owned-position audit, still without live permissions.

This does NOT place orders, call protected commits, write SQL, manage
credentials, schedule work, alert Discord, or deploy to Render.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from app.t100_live_four_slot_readonly_cli import run_readonly_four_slot_audit
from app.t100_live_four_slot_restart_triage import (
    FourSlotRestartTriage, PostgresFourSlotRestartTriage, RecoveryAttempt,
)


def _blocked(
    status: str, *, active_slots: int | None = None,
    pending_attempts: int | None = None,
    durable_claims: int | None = None,
    inventory_status: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "active_v40_slots": active_slots,
        "pending_v40_attempts": pending_attempts,
        "durable_v40_dispatch_claims": durable_claims,
        "inventory_status": inventory_status,
        "account_inventory_verified": False,
        "native_stop_execution_certified": False,
        "cross_margin_liquidation_certified": False,
        "manual_position_adopted": False,
        "order_retry_allowed": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "database_writes_sent": False,
        "read_only": True,
    }


def _triage_contract_verified(status: FourSlotRestartTriage) -> bool:
    """Reject contradictory counters, privileged or forged observer results."""
    if type(status) is not FourSlotRestartTriage:
        return False
    if (
        type(status.active_slots) is not int
        or type(status.pending_attempts) is not int
        or type(status.durable_claims) is not int
        or not 0 <= status.active_slots <= 4
        or not 0 <= status.pending_attempts <= status.active_slots
        or not 0 <= status.durable_claims <= status.active_slots
        or type(status.attempts) is not tuple
        or len(status.attempts) != status.active_slots
        or status.read_only is not True
        or status.retry_allowed is not False
        or status.entry_allowed is not False
        or status.live_authorization is not False
        or status.mexc_orders_sent is not False
        or status.database_writes_sent is not False
        or status.manual_position_adopted is not False
        or status.account_inventory_verified is not False
        or status.native_stop_protection_verified is not False
    ):
        return False
    if any(
        type(item) is not RecoveryAttempt
        or type(item.claim_present) is not bool
        or item.action != "EXACT_ORIGIN_RECONCILIATION_ONLY"
        or item.retry_allowed is not False
        or item.manual_position_adopted is not False
        for item in status.attempts
    ):
        return False
    expected_pending = sum(
        item.slot_state != "PROTECTED" for item in status.attempts
    )
    expected_claimed = sum(
        item.claim_present for item in status.attempts
    )
    if (
        status.pending_attempts != expected_pending
        or status.durable_claims != expected_claimed
        or status.status != (
            "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY"
            if expected_pending else
            "RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING"
        )
    ):
        return False
    return True


async def inspect_restart_db_gate(
    *, triage: PostgresFourSlotRestartTriage,
    env: Mapping[str, str],
) -> dict[str, Any] | FourSlotRestartTriage:
    """Only a fresh, internally consistent SELECT result may permit GETs.

    Returns a redacted blocker for unsafe/pending evidence, otherwise
    the validated read-only triage snapshot. A clean snapshot allows
    GET-only observation, NOT a trade, POST, reservation or stop edit.
    """
    try:
        require_readonly_controls(env)
    except (CrossRiskDiagnosticHalted, ValueError, TypeError):
        return _blocked("RESTART_READONLY_PAPER_KILL_REQUIRED")
    if type(triage) is not PostgresFourSlotRestartTriage:
        return _blocked("RESTART_READONLY_READER_OR_MODE_INVALID")
    try:
        status = await triage.inspect()
    except Exception:
        return _blocked("RESTART_READONLY_TRIAGE_READ_FAILED")
    if status.status not in (
        "RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING",
        "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY",
    ):
        return _blocked("RESTART_READONLY_DURABLE_STATE_UNVERIFIED")
    if not _triage_contract_verified(status):
        return _blocked("RESTART_READONLY_TRIAGE_CONTRACT_UNVERIFIED")
    if status.pending_attempts:
        return _blocked(
            "RESTART_PENDING_ATTEMPT_EXACT_ORIGIN_RECONCILE_ONLY",
            active_slots=status.active_slots,
            pending_attempts=status.pending_attempts,
            durable_claims=status.durable_claims,
        )
    return status


async def run_four_slot_restart_readonly_cycle(
    *, triage: PostgresFourSlotRestartTriage,
    ledger: Any, client: Any,
    env: Mapping[str, str],
    shared_manual_observation: bool = True,
) -> dict[str, Any]:
    """A single nonauthorizing cycle; no implicit retries of any kind."""
    if type(shared_manual_observation) is not bool:
        return _blocked("RESTART_READONLY_READER_OR_MODE_INVALID")
    result = await inspect_restart_db_gate(triage=triage, env=env)
    if type(result) is dict:
        return result

    # This fresh triage gate deliberately runs again if the CLI already
    # checked DB before constructing its GET client; a changed journal
    # must never be overlooked between those two observations.
    fields = {
        "active_slots": result.active_slots,
        "pending_attempts": result.pending_attempts,
        "durable_claims": result.durable_claims,
    }
    try:
        inventory = await run_readonly_four_slot_audit(
            ledger=ledger, client=client, env=env,
            shared_manual_observation=shared_manual_observation,
        )
    except Exception:
        return _blocked("RESTART_READONLY_INVENTORY_READ_FAILED", **fields)

    if (
        type(inventory) is not dict
        or inventory.get("entry_allowed") is not False
        or inventory.get("live_authorization") is not False
        or inventory.get("mexc_orders_sent") is not False
        or inventory.get("database_writes_sent") is not False
    ):
        return _blocked("RESTART_READONLY_INVENTORY_CONTRACT_UNVERIFIED", **fields)

    inv_status = inventory.get("status")
    # A manual position can appear in the shared-mode observation; never
    # adopt it as T100 collateral, even with an apparent native SL.
    if inv_status in (
        "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
    ) and inventory.get("manual_positions_observed", 0) > 0:
        return _blocked(
            "RESTART_MANUAL_POSITION_REVIEW_REQUIRED",
            inventory_status=inv_status, **fields,
        )
    if inv_status not in (
        "FLAT_OBSERVED_NONAUTHORIZING",
        "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
    ):
        return _blocked(
            "RESTART_ACCOUNT_INVENTORY_UNVERIFIED",
            inventory_status=inv_status if type(inv_status) is str else None,
            **fields,
        )
    return _blocked(
        "RESTART_OBSERVED_NONAUTHORIZING",
        inventory_status=inv_status, **fields,
    )
