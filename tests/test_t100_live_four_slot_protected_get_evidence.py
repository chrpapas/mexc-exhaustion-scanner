"""Four-slot staged GET-only order/deal/protection bridge regressions.

In-memory venue fixtures only: no exchange requests, no credentials, no
network, no DB writes or live trading. Source ownership remains staged and
unresolved even after an offline protected bundle is prepared.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import unittest

from app.t100_live_four_slot_dispatch_claim_reader import (
    DispatchClaimRecovery,
)
from app.t100_live_four_slot_protected_get_evidence import (
    observe_and_prepare_staged_protection_get_only,
    observe_claimed_staged_protection_get_only,
)
from tests.test_t100_live_four_slot_attempt_outcome import ProtectedOutcomeTests
from tests.test_t100_live_v40_protected_finalization import deal
from tests.test_t100_live_four_slot_snapshot_coordinator import Completeness, report


class ProtectedGetBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = ProtectedOutcomeTests()
        await source.asyncSetUp()
        self.stage, self.intent, self.before = (
            source.stage, source.intent, source.before,
        )
        self.venue = source.venue
        self.venue.orders[(self.intent.symbol, self.intent.external_oid)][
            "feeCurrency"
        ] = "USDT"
        self.complete = Completeness(report(4))
        self.deal_reads = 0
        self.deal_values = [[deal()], [deal()]]
        self.pg = source.pg

        async def fetch_deals(order_id):
            self.deal_reads += 1
            if order_id != "990":
                raise AssertionError("wrong order")
            return deepcopy(
                self.deal_values[min(self.deal_reads - 1, 1)]
            )
        self.venue.order_deal_details = fetch_deals

    async def run_bridge(self, stages=None, intents=None):
        return await observe_and_prepare_staged_protection_get_only(
            stages=[self.stage] if stages is None else stages,
            intents=[self.intent] if intents is None else intents,
            pre_attempt_witness=self.before,
            venue=self.venue, completeness=self.complete,
        )

    async def test_full_fourth_slot_prepares_offline_bundle_with_exact_deal_chain(self):
        r = await self.run_bridge()
        self.assertEqual(r.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING")
        self.assertTrue(r.two_bookends_equal)
        self.assertEqual(r.orders_observed, 1)
        self.assertEqual(r.deal_histories_observed, 1)
        self.assertEqual(self.venue.order_reads, 2)
        self.assertEqual(self.venue.positions_reads, 2)
        self.assertEqual(self.venue.stop_reads, 2)
        self.assertEqual(self.deal_reads, 2)
        self.assertEqual(self.complete.calls, 2)
        self.assertEqual(len(r.prepared_batch.entries), 1)
        item = r.prepared_batch.entries[0]
        self.assertEqual(item.order_id, "990")
        self.assertEqual(item.position_id, "900")
        self.assertEqual(item.checkpoint.lifecycle_state, "OPEN")
        self.assertEqual(item.checkpoint.stop_order_id, "999")
        self.assertEqual(item.fill_set.fills[0].trade_id, "2001")
        self.assertFalse(r.prepared_batch.durable_ack_persisted)
        self.assertFalse(r.prepared_batch.durable_checkpoint_persisted)
        self.assertFalse(r.durable_ack_persisted)
        self.assertFalse(r.exchange_atomicity_verified)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.mexc_orders_sent)
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")

    async def test_claim_bound_bridge_rejects_missing_claim_before_all_venue_gets(self):
        class Claims:
            calls = 0
            async def inspect(self, stages):
                self.calls += 1
                return DispatchClaimRecovery("DISPATCH_CLAIMS_MISSING_NO_RETRY")

        claims = Claims()
        result = await observe_claimed_staged_protection_get_only(
            stages=[self.stage], intents=[self.intent],
            pre_attempt_witness=self.before,
            claim_reader=claims, venue=self.venue,
            completeness=self.complete,
        )
        self.assertEqual(claims.calls, 1)
        self.assertEqual(
            result.status, "PROTECTED_GET_DISPATCH_CLAIM_NOT_VERIFIED_NO_RETRY",
        )
        self.assertIsNone(result.prepared_batch)
        self.assertFalse(result.dispatch_claims_verified)
        self.assertEqual(self.venue.positions_reads, 0)
        self.assertEqual(self.venue.order_reads, 0)
        self.assertEqual(self.deal_reads, 0)
        self.assertEqual(self.complete.calls, 0)

    async def test_claim_bound_bridge_prepares_only_after_exact_claim(self):
        class Claims:
            async def inspect(self, stages):
                return DispatchClaimRecovery(
                    status="DISPATCH_CLAIMS_PRESENT_RECONCILE_ONLY",
                    claimed_attempts=1,
                    all_claims_exact_and_durable=True,
                )

        result = await observe_claimed_staged_protection_get_only(
            stages=[self.stage], intents=[self.intent],
            pre_attempt_witness=self.before,
            claim_reader=Claims(), venue=self.venue,
            completeness=self.complete,
        )
        self.assertEqual(
            result.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        )
        self.assertTrue(result.dispatch_claims_verified)
        self.assertEqual(result.orders_observed, 1)
        self.assertEqual(result.deal_histories_observed, 1)
        self.assertTrue(result.two_bookends_equal)
        self.assertFalse(result.durable_ack_persisted)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)

    async def test_claim_bound_bridge_fails_closed_if_claim_reader_crashes(self):
        class Claims:
            async def inspect(self, stages):
                raise ConnectionError("database connection unavailable")

        result = await observe_claimed_staged_protection_get_only(
            stages=[self.stage], intents=[self.intent],
            pre_attempt_witness=self.before,
            claim_reader=Claims(), venue=self.venue,
            completeness=self.complete,
        )
        self.assertEqual(
            result.status, "PROTECTED_GET_DISPATCH_CLAIM_READ_FAILED_NO_RETRY",
        )
        self.assertEqual(self.venue.positions_reads, 0)
        self.assertEqual(self.deal_reads, 0)
        self.assertFalse(result.entry_allowed)

    async def test_exact_order_missing_blocks_before_deal_get(self):
        self.venue.orders.clear()
        r = await self.run_bridge()
        self.assertEqual(r.status, "PROTECTED_GET_ORDER_UNKNOWN_NO_RETRY")
        self.assertEqual(self.deal_reads, 0)
        self.assertIsNone(r.prepared_batch)

    async def test_bookended_deal_mutation_blocks_without_prepared_checkpoint(self):
        self.deal_values[1][0]["fee"] = D("0.4")
        r = await self.run_bridge()
        self.assertEqual(r.status, "PROTECTED_GET_BOOKEND_OR_DEALS_DRIFT_NO_RETRY")
        self.assertIsNone(r.prepared_batch)
        self.assertEqual(self.deal_reads, 2)

    async def test_bookended_position_change_blocks_even_if_count_same(self):
        self.venue.last_positions[-1]["holdVol"] = D("249")
        r = await self.run_bridge()
        self.assertEqual(r.status, "PROTECTED_GET_BOOKEND_OR_DEALS_DRIFT_NO_RETRY")
        self.assertIsNone(r.prepared_batch)

    async def test_missing_empty_or_timeout_deal_history_never_certifies(self):
        async def missing(_):
            return []
        self.venue.order_deal_details = missing
        self.assertEqual((await self.run_bridge()).status,
                         "PROTECTED_GET_DEALS_UNKNOWN_NO_RETRY")
        async def timeout(_):
            raise TimeoutError("do not include exchange payload")
        self.venue.order_deal_details = timeout
        self.assertEqual((await self.run_bridge()).status,
                         "PROTECTED_GET_READ_OR_FINGERPRINT_FAILED_NO_RETRY")

    async def test_duplicate_or_partial_fills_rejected(self):
        for rows in (
            [deal(), deal()],
            [deal(vol=D("249"))],
            [deal(feeCurrency="MX")],
            [deal(category=2)],
        ):
            with self.subTest(rows=rows[0].get("category")):
                await self.asyncSetUp()
                self.deal_values = [deepcopy(rows), deepcopy(rows)]
                r = await self.run_bridge()
                self.assertEqual(
                    r.status, "PROTECTED_GET_FILL_OR_NATIVE_STOP_UNVERIFIED_NO_RETRY"
                )
                self.assertIsNone(r.prepared_batch)

    async def test_stop_undercoverage_and_unowned_position_fail_closed(self):
        self.venue.first_stops[-1]["vol"] = D("100")
        self.venue.last_stops[-1]["vol"] = D("100")
        r = await self.run_bridge()
        self.assertEqual(
            r.status, "PROTECTED_GET_FILL_OR_NATIVE_STOP_UNVERIFIED_NO_RETRY"
        )
        await self.asyncSetUp()
        self.venue.first_positions[0]["symbol"] = "UNOWNED_USDT"
        self.venue.last_positions[0]["symbol"] = "UNOWNED_USDT"
        r = await self.run_bridge()
        self.assertEqual(
            r.status, "PROTECTED_GET_FILL_OR_NATIVE_STOP_UNVERIFIED_NO_RETRY"
        )

    async def test_stage_prevalidation_does_not_touch_venue(self):
        bad = replace(self.stage, external_oid="wrong")
        r = await self.run_bridge(stages=[bad])
        self.assertEqual(r.status, "PROTECTED_GET_STAGED_IDENTITY_UNVERIFIED")
        self.assertEqual(self.venue.positions_reads, 0)
        self.assertIsNone(r.prepared_batch)

    def test_get_only_source_contains_no_mutation_interfaces(self):
        import inspect
        import app.t100_live_four_slot_protected_get_evidence as module
        text = inspect.getsource(module)
        for forbidden in (
            "._private_post(", "submit_short_with_stop(",
            "PostgresProtectedV40Commit(", "LiveMutationPermit(",
            ".persist(", "asyncpg.", "httpx.",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
