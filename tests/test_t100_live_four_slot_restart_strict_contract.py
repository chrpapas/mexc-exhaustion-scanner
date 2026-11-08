"""Offline regression: V40 restart refuses spoofed slot and inventory evidence.

No exchange client, DB credentials, POST or SQL writes are used.
"""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from app.t100_live_four_slot_restart_supervisor import (
    _triage_contract_verified, run_four_slot_restart_readonly_cycle,
)
from app.t100_live_four_slot_restart_triage import (
    FourSlotRestartTriage, RecoveryAttempt, PostgresFourSlotRestartTriage,
)
from tests.test_t100_live_four_slot_restart_triage import FakePool
from tests.test_t100_live_four_slot_readonly_cli import PAPER_KILLED


def _protected():
    return RecoveryAttempt(
        signal_id=42, symbol="EXAMPLE_USDT",
        external_oid="one-opaque-origin", slot_state="PROTECTED",
        intent_state="ACK", claim_present=True,
    )


def _triage(*attempts):
    return FourSlotRestartTriage(
        status="RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING",
        active_slots=len(attempts), pending_attempts=0,
        durable_claims=sum(a.claim_present for a in attempts),
        attempts=tuple(attempts),
    )


def test_genuine_protected_snapshot_is_valid():
    assert _triage_contract_verified(_triage(_protected()))


@pytest.mark.parametrize("changes", [
    {"signal_id": True},
    {"signal_id": 0},
    {"symbol": "BAD!!"},
    {"symbol": ""},
    {"external_oid": ""},
    {"external_oid": "opaque/invalid"},
    {"slot_state": "RELEASED"},
    {"slot_state": "UNRECOGNIZED"},
    {"intent_state": "UNKNOWN"},
    {"intent_state": "REVIEW"},
    {"claim_present": False},
])
def test_forged_protected_slot_refused(changes):
    row = replace(_protected(), **changes)
    # Rebuild the counters to make the forged data look internally consistent.
    assert not _triage_contract_verified(_triage(row))


def test_duplicate_ids_symbols_and_order_origins_refused():
    original = _protected()
    for duplicate in (
        replace(original, external_oid="second-origin", symbol="SECOND_USDT"),
        replace(original, signal_id=43, external_oid="second-origin"),
        replace(original, signal_id=43, symbol="SECOND_USDT"),
    ):
        assert not _triage_contract_verified(_triage(original, duplicate))


def test_reserved_slot_cannot_claim_or_have_intent():
    baseline = RecoveryAttempt(
        signal_id=42, symbol="EXAMPLE_USDT",
        external_oid="origin-one", slot_state="RESERVED",
        intent_state=None, claim_present=False,
    )
    safe = FourSlotRestartTriage(
        status="RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY",
        active_slots=1, pending_attempts=1, durable_claims=0,
        attempts=(baseline,),
    )
    assert _triage_contract_verified(safe)
    for variant in (
        replace(baseline, claim_present=True),
        replace(baseline, intent_state="SUBMITTING"),
    ):
        altered = replace(
            safe,
            attempts=(variant,),
            durable_claims=int(variant.claim_present),
        )
        assert not _triage_contract_verified(altered)


@pytest.mark.parametrize("alter", [
    {"read_only": False},
    {"native_stop_exchange_atomicity_certified": True},
    {"durable_entry_executor_certified": True},
    {"manual_positions_adopted_by_t100": True},
    {"entry_allowed": True},
    {"live_authorization": True},
    {"mexc_orders_sent": True},
    {"database_writes_sent": True},
])
def test_privileged_inventory_claims_block_restart(alter):
    inventory = {
        "status": "FLAT_OBSERVED_NONAUTHORIZING",
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "database_writes_sent": False,
        "read_only": True,
        "native_stop_exchange_atomicity_certified": False,
        "durable_entry_executor_certified": False,
    }
    inventory.update(alter)
    triage = PostgresFourSlotRestartTriage(FakePool([]))
    with patch(
        "app.t100_live_four_slot_restart_supervisor.run_readonly_four_slot_audit",
        new=AsyncMock(return_value=inventory),
    ):
        report = asyncio.run(run_four_slot_restart_readonly_cycle(
            triage=triage, ledger=object(), client=object(),
            env=PAPER_KILLED, shared_manual_observation=False,
        ))
    assert report["status"] == "RESTART_READONLY_INVENTORY_CONTRACT_UNVERIFIED"
    assert report["order_retry_allowed"] is False
    assert report["live_authorization"] is False
    assert report["mexc_orders_sent"] is False
    assert report["database_writes_sent"] is False


@pytest.mark.parametrize("status,owned,expected", [
    ("FLAT_OBSERVED_NONAUTHORIZING", 0,
     "RESTART_PROTECTED_SLOT_INVENTORY_MISMATCH_NO_RETRY"),
    ("NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING", 0,
     "RESTART_PROTECTED_SLOT_INVENTORY_MISMATCH_NO_RETRY"),
    ("SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING", 0,
     "RESTART_PROTECTED_SLOT_INVENTORY_MISMATCH_NO_RETRY"),
    ("NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING", 1,
     "RESTART_OBSERVED_NONAUTHORIZING"),
])
def test_protected_slot_requires_owned_venue_position(
    status, owned, expected,
):
    durable_snapshot = _triage(_protected())
    inventory = {
        "status": status,
        "owned_four_slot_positions_observed": owned,
        "manual_positions_observed": 0,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "database_writes_sent": False,
        "read_only": True,
        "native_stop_exchange_atomicity_certified": False,
        "durable_entry_executor_certified": False,
    }
    triage = PostgresFourSlotRestartTriage(FakePool([]))
    with (
        patch.object(
            PostgresFourSlotRestartTriage, "inspect",
            new=AsyncMock(return_value=durable_snapshot),
        ),
        patch(
            "app.t100_live_four_slot_restart_supervisor.run_readonly_four_slot_audit",
            new=AsyncMock(return_value=inventory),
        ),
    ):
        result = asyncio.run(run_four_slot_restart_readonly_cycle(
            triage=triage, ledger=object(), client=object(),
            env=PAPER_KILLED, shared_manual_observation=False,
        ))
    assert result["status"] == expected
    assert result["order_retry_allowed"] is False
    assert result["entry_allowed"] is False
    assert result["live_authorization"] is False
