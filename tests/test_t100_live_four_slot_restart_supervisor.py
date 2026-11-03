"""Read-only restart supervisor: no retry, no writes, manual positions isolated."""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.t100_live_four_slot_restart_supervisor import (
    run_four_slot_restart_readonly_cycle,
)
from app.t100_live_four_slot_restart_triage import (
    FourSlotRestartTriage, PostgresFourSlotRestartTriage,
    RecoveryAttempt,
)
from tests.test_t100_live_four_slot_restart_triage import (
    FakePool, sample,
)
from tests.test_t100_live_four_slot_readonly_cli import PAPER_KILLED
from tests.test_t100_live_four_slot_mexc_readonly import (
    MockMexcGetOnly, prepare_rows,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import Ledger


class FourSlotReadOnlySupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_owned_protected_slot_only_observes_get(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        r = await run_four_slot_restart_readonly_cycle(
            triage=PostgresFourSlotRestartTriage(FakePool([])),
            ledger=Ledger(snap), client=client, env=PAPER_KILLED,
            shared_manual_observation=False,
        )
        self.assertEqual(r["status"], "RESTART_OBSERVED_NONAUTHORIZING")
        self.assertEqual(
            r["inventory_status"],
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertGreater(len(client.calls), 0)
        self.assertTrue(
            all("/private/" in path for path, _ in client.calls)
        )
        self.assertFalse(r["order_retry_allowed"])
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["mexc_orders_sent"])
        self.assertFalse(r["database_writes_sent"])

    async def test_staged_or_claimed_ambiguous_attempt_blocks_all_venue_get(self):
        snap, orders, positions, stops = prepare_rows(1)
        for claimed in (True, False):
            with self.subTest(claimed=claimed):
                rows = [sample()]
                if not claimed:
                    rows[0].update(
                        claim_signal_id=None, claim_external_oid=None,
                        claim_holder_id=None, claim_fencing_token=None,
                        claim_state=None,
                    )
                client = MockMexcGetOnly(orders, positions, stops)
                r = await run_four_slot_restart_readonly_cycle(
                    triage=PostgresFourSlotRestartTriage(FakePool(rows)),
                    ledger=Ledger(snap), client=client, env=PAPER_KILLED,
                )
                self.assertEqual(
                    r["status"],
                    "RESTART_PENDING_ATTEMPT_EXACT_ORIGIN_RECONCILE_ONLY",
                )
                self.assertEqual(r["pending_v40_attempts"], 1)
                self.assertEqual(r["durable_v40_dispatch_claims"], int(claimed))
                self.assertEqual(client.calls, [])
                self.assertFalse(r["order_retry_allowed"])

    async def test_contradictory_or_privileged_triage_never_reaches_venue(self):
        snap, orders, positions, stops = prepare_rows(1)
        base = FourSlotRestartTriage(
            status="RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING",
            active_slots=1, pending_attempts=0, durable_claims=1,
            attempts=(RecoveryAttempt(
                signal_id=42, symbol="EXAMPLE_USDT",
                external_oid="opaque-origin", slot_state="PROTECTED",
                intent_state="ACK", claim_present=True,
            ),),
        )
        invalids = (
            replace(base, status="RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY"),
            replace(base, pending_attempts=1),
            replace(base, durable_claims=0),
            replace(base, active_slots=True),
            replace(base, active_slots=2),
            replace(base, read_only=False),
            replace(base, mexc_orders_sent=True),
            replace(base, database_writes_sent=True),
            replace(base, manual_position_adopted=True),
            replace(base, account_inventory_verified=True),
            replace(base, native_stop_protection_verified=True),
            replace(base, attempts=(replace(
                base.attempts[0], action="RETRY_EXCHANGE_POST",
            ),)),
            replace(base, attempts=(replace(
                base.attempts[0], claim_present=False,
            ),)),
        )
        for bad in invalids:
            with self.subTest(bad=bad):
                client = MockMexcGetOnly(orders, positions, stops)
                triage = PostgresFourSlotRestartTriage(FakePool([]))
                with patch.object(
                    PostgresFourSlotRestartTriage, "inspect",
                    new=AsyncMock(return_value=bad),
                ):
                    r = await run_four_slot_restart_readonly_cycle(
                        triage=triage, ledger=Ledger(snap),
                        client=client, env=PAPER_KILLED,
                    )
                self.assertEqual(
                    r["status"],
                    "RESTART_READONLY_TRIAGE_CONTRACT_UNVERIFIED",
                )
                self.assertEqual(client.calls, [])
                self.assertFalse(r["order_retry_allowed"])

    async def test_mismatched_claim_blocks_before_any_get(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        r = await run_four_slot_restart_readonly_cycle(
            triage=PostgresFourSlotRestartTriage(FakePool([
                sample(claim_external_oid="different-order-origin"),
            ])),
            ledger=Ledger(snap), client=client, env=PAPER_KILLED,
        )
        self.assertEqual(r["status"], "RESTART_READONLY_DURABLE_STATE_UNVERIFIED")
        self.assertEqual(client.calls, [])
        self.assertFalse(r["entry_allowed"])

    async def test_wrong_controls_block_before_db_or_venue_read(self):
        snap, orders, positions, stops = prepare_rows(1)
        for env in (
            {}, {**PAPER_KILLED, "T100_EXECUTION_MODE": "live"},
            {**PAPER_KILLED, "T100_LIVE_ORDER_API_ENABLED": "true"},
            {**PAPER_KILLED, "T100_LIVE_KILL_SWITCH": "false"},
        ):
            with self.subTest(env=env):
                pool = FakePool([sample()])
                client = MockMexcGetOnly(orders, positions, stops)
                result = await run_four_slot_restart_readonly_cycle(
                    triage=PostgresFourSlotRestartTriage(pool),
                    ledger=Ledger(snap), client=client, env=env,
                )
                self.assertEqual(
                    result["status"], "RESTART_READONLY_PAPER_KILL_REQUIRED",
                )
                self.assertEqual(pool.conn.calls, [])
                self.assertEqual(client.calls, [])

    async def test_unprotected_position_cannot_look_ready(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, [])
        r = await run_four_slot_restart_readonly_cycle(
            triage=PostgresFourSlotRestartTriage(FakePool([])),
            ledger=Ledger(snap), client=client,
            env=PAPER_KILLED, shared_manual_observation=False,
        )
        self.assertEqual(r["status"], "RESTART_ACCOUNT_INVENTORY_UNVERIFIED")
        self.assertEqual(r["inventory_status"], "POSITION_STOP_COUNT_MISMATCH")
        self.assertFalse(r["native_stop_execution_certified"])
        self.assertFalse(r["live_authorization"])

    async def test_external_manual_position_can_never_be_adopted(self):
        owned, _, _, _ = prepare_rows(0)
        _, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        r = await run_four_slot_restart_readonly_cycle(
            triage=PostgresFourSlotRestartTriage(FakePool([])),
            ledger=Ledger(owned), client=client,
            env=PAPER_KILLED,
            shared_manual_observation=True,
        )
        self.assertIn(
            r["status"],
            (
                "RESTART_MANUAL_POSITION_REVIEW_REQUIRED",
                "RESTART_ACCOUNT_INVENTORY_UNVERIFIED",
            ),
        )
        self.assertFalse(r["manual_position_adopted"])
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])

    def test_module_has_no_order_submit_or_database_write_surface(self):
        import inspect
        import app.t100_live_four_slot_restart_supervisor as mod
        text = inspect.getsource(mod)
        for forbidden in (
            "._private_post(", "submit_short_with_stop(",
            "PostgresProtectedV40Commit(", "LiveMutationPermit",
            ".persist_claimed(", ".claim(", ".execute(",
            "trigger_deploy(",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
