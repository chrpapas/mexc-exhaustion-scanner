"""Pure offline restart triage tests, including all refuse-to-retry paths."""
from __future__ import annotations

from contextlib import asynccontextmanager
import unittest

from app.t100_live_four_slot_restart_triage import (
    PostgresFourSlotRestartTriage, ACTIVE_SLOT_RECOVERY_SQL,
)


def sample(**changes):
    row = {
        "signal_id": 42, "symbol": "EXAMPLE_USDT",
        "external_oid": "opaque-order-origin",
        "slot_state": "SUBMITTING",
        "slot_holder_id": "worker-A", "slot_fencing_token": 11,
        "intent_signal_id": 42, "intent_symbol": "EXAMPLE_USDT",
        "intent_external_oid": "opaque-order-origin",
        "intent_state": "SUBMITTING",
        "claim_signal_id": 42,
        "claim_external_oid": "opaque-order-origin",
        "claim_holder_id": "worker-A", "claim_fencing_token": 11,
        "claim_state": "POST_POSSIBLE_UNRESOLVED",
    }
    row.update(changes)
    return row


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.role = "t100_live_executor_runtime"
        self.db = "mexc_exhaustion"
        self.readonly = "on"
        self.isolation = "repeatable read"
        self.fail = False

    @asynccontextmanager
    async def transaction(self, *, isolation, readonly):
        assert isolation == "repeatable_read" and readonly is True
        yield self

    async def fetchval(self, query):
        self.calls.append(query)
        if self.fail:
            raise ConnectionError("secret DB URL")
        if "transaction_read_only" in query:
            return self.readonly
        if "transaction_isolation" in query:
            return self.isolation
        if "current_database()" in query:
            return self.db
        if "current_user" in query:
            return self.role
        raise AssertionError(query)

    async def fetch(self, query):
        self.calls.append(query)
        if self.fail:
            raise ConnectionError("secret DB URL")
        assert query == ACTIVE_SLOT_RECOVERY_SQL
        return self.rows


class FakePool:
    def __init__(self, rows):
        self.conn = FakeConn(rows)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class FourSlotRestartTriageTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_active_slots_is_still_nonauthorizing(self):
        p = FakePool([])
        r = await PostgresFourSlotRestartTriage(p).inspect()
        self.assertEqual(r.status, "RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING")
        self.assertEqual(r.active_slots, 0)
        self.assertEqual(r.attempts, ())
        self.assertFalse(r.account_inventory_verified)
        self.assertFalse(r.native_stop_protection_verified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.retry_allowed)
        self.assertFalse(r.database_writes_sent)

    async def test_mixed_pending_protected_and_reserved_never_authorize(self):
        p = FakePool([
            sample(),
            sample(
                signal_id=43, symbol="EXAMPLE2_USDT",
                external_oid="origin-protected",
                slot_state="PROTECTED",
                intent_signal_id=43, intent_symbol="EXAMPLE2_USDT",
                intent_external_oid="origin-protected",
                intent_state="ACK", claim_signal_id=43,
                claim_external_oid="origin-protected",
            ),
            sample(
                signal_id=44, symbol="EXAMPLE3_USDT",
                external_oid="reserved-no-order",
                slot_state="RESERVED",
                intent_signal_id=None, intent_symbol=None,
                intent_external_oid=None, intent_state=None,
                claim_signal_id=None, claim_external_oid=None,
                claim_holder_id=None, claim_fencing_token=None,
                claim_state=None,
            ),
        ])
        r = await PostgresFourSlotRestartTriage(p).inspect()
        self.assertEqual(r.status, "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY")
        self.assertEqual(r.active_slots, 3)
        self.assertEqual(r.pending_attempts, 2)
        self.assertEqual(r.durable_claims, 2)
        self.assertEqual(len(r.attempts), 3)
        self.assertTrue(all(not x.retry_allowed for x in r.attempts))
        self.assertTrue(all(not x.manual_position_adopted for x in r.attempts))
        self.assertFalse(r.mexc_orders_sent)
        summary = r.safe_summary()
        self.assertNotIn("EXAMPLE_USDT", str(summary))
        self.assertNotIn("opaque-order-origin", str(summary))
        self.assertNotIn("attempts", summary)
        self.assertFalse(summary["entry_allowed"])

    async def test_missing_claim_is_not_evidence_that_post_never_happened(self):
        r = await PostgresFourSlotRestartTriage(FakePool([
            sample(
                claim_signal_id=None, claim_external_oid=None,
                claim_holder_id=None, claim_fencing_token=None,
                claim_state=None,
            )
        ])).inspect()
        self.assertEqual(r.status, "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY")
        self.assertEqual(r.durable_claims, 0)
        self.assertEqual(r.attempts[0].action, "EXACT_ORIGIN_RECONCILIATION_ONLY")
        self.assertFalse(r.retry_allowed)
        self.assertFalse(r.entry_allowed)

    async def test_acked_or_protected_without_claim_blocks(self):
        for state, intent_state in (("ACK", "ACK"), ("PROTECTED", "ACK")):
            with self.subTest(state=state):
                r = await PostgresFourSlotRestartTriage(FakePool([
                    sample(
                        slot_state=state, intent_state=intent_state,
                        claim_signal_id=None, claim_external_oid=None,
                        claim_holder_id=None, claim_fencing_token=None,
                        claim_state=None,
                    )
                ])).inspect()
                self.assertEqual(r.status, "RESTART_TRIAGE_PROTECTED_WITHOUT_CLAIM")
                self.assertFalse(r.entry_allowed)

    async def test_all_provenance_mismatches_block(self):
        for bad in (
            {"symbol": ""},
            {"slot_state": "RELEASED"},
            {"slot_fencing_token": True},
            {"intent_symbol": "WRONG_USDT"},
            {"intent_external_oid": "other"},
            {"intent_signal_id": 999},
            {"claim_external_oid": "another-origin"},
            {"claim_holder_id": "wrong-worker"},
            {"claim_fencing_token": 12},
            {"claim_state": "SETTLED"},
            {"slot_state": "SUBMITTING", "intent_state": "UNKNOWN"},
            {"slot_state": "PROTECTED", "intent_state": "SUBMITTING"},
        ):
            with self.subTest(bad=bad):
                r = await PostgresFourSlotRestartTriage(FakePool([
                    sample(**bad)
                ])).inspect()
                self.assertNotIn(
                    r.status,
                    (
                        "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY",
                        "RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING",
                    ),
                )
                self.assertEqual(r.attempts, ())
                self.assertFalse(r.live_authorization)
                self.assertFalse(r.retry_allowed)

    async def test_too_many_and_duplicate_slots_block(self):
        too_many = [
            sample(
                signal_id=42+i, symbol=f"E{i}_USDT",
                external_oid=f"origin-{i}",
                intent_signal_id=42+i, intent_symbol=f"E{i}_USDT",
                intent_external_oid=f"origin-{i}", claim_signal_id=42+i,
                claim_external_oid=f"origin-{i}",
            )
            for i in range(5)
        ]
        r = await PostgresFourSlotRestartTriage(FakePool(too_many)).inspect()
        self.assertEqual(r.status, "RESTART_TRIAGE_ACCOUNT_OVER_FOUR_SLOTS")
        r = await PostgresFourSlotRestartTriage(FakePool([
            sample(), sample(signal_id=99, intent_signal_id=99,
                             claim_signal_id=99)
        ])).inspect()
        self.assertEqual(r.status, "RESTART_TRIAGE_SLOT_IDENTITY_DRIFT")

    async def test_wrong_role_read_write_or_database_failure_are_nonauthorizing(self):
        for setting, bad in (
            ("role", "postgres"),
            ("readonly", "off"),
            ("isolation", "read committed"),
            ("db", "other_database"),
        ):
            with self.subTest(setting=setting):
                p = FakePool([sample()])
                setattr(p.conn, setting, bad)
                r = await PostgresFourSlotRestartTriage(p).inspect()
                self.assertEqual(
                    r.status,
                    "RESTART_TRIAGE_RESTRICTED_CONTEXT_UNVERIFIED",
                )
                self.assertNotIn(ACTIVE_SLOT_RECOVERY_SQL, p.conn.calls)
        p = FakePool([sample()])
        p.conn.fail = True
        r = await PostgresFourSlotRestartTriage(p).inspect()
        self.assertEqual(
            r.status, "RESTART_TRIAGE_DATABASE_READ_FAILED_NO_RETRY",
        )
        self.assertNotIn("secret DB URL", str(r.safe_summary()))
        self.assertFalse(r.entry_allowed)


if __name__ == "__main__":
    unittest.main()
