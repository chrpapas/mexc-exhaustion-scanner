"""Two-bookend pending V40 external order GET diagnostic: zero mutation."""
from __future__ import annotations

from dataclasses import replace
import unittest

from app.t100_live_four_slot_pending_origin_get import (
    PendingOriginGetReport, observe_pending_origins_get_only,
)
from app.t100_live_four_slot_restart_triage import (
    FourSlotRestartTriage, RecoveryAttempt,
)
from tests.test_t100_live_four_slot_mexc_readonly import (
    MockMexcGetOnly, prepare_rows,
)


def pending(orders, *, state="UNKNOWN", claimed=False):
    attempts = tuple(
        RecoveryAttempt(
            signal_id=40+i, symbol=order["symbol"],
            external_oid=order["externalOid"], slot_state=state,
            intent_state=(
                None if state == "RESERVED" else
                "SUBMITTING" if state == "SUBMITTING" else "UNKNOWN"
            ),
            claim_present=claimed,
        )
        for i, order in enumerate(orders)
    )
    return FourSlotRestartTriage(
        status="RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY",
        active_slots=len(attempts),
        pending_attempts=len(attempts),
        durable_claims=len(attempts) if claimed else 0,
        attempts=attempts,
    )


class PendingOriginGetTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_order_is_observation_not_fill_or_stop_proof(self):
        _, orders, _, _ = prepare_rows(1)
        client = MockMexcGetOnly(orders, [], [])
        report = await observe_pending_origins_get_only(
            triage=pending(orders, claimed=True), client=client,
        )
        self.assertEqual(
            report.status,
            "PENDING_ORIGIN_ORDERS_SEEN_UNPROTECTED_NONAUTHORIZING",
        )
        self.assertEqual(report.origins_checked, 1)
        self.assertEqual(report.origin_orders_seen, 1)
        self.assertEqual(report.origins_absent_unknown, 0)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            all("/api/v1/private/order/external/" in path
                for path, _ in client.calls)
        )
        self.assertFalse(report.order_fill_verified)
        self.assertFalse(report.native_stop_protection_verified)
        self.assertFalse(report.account_inventory_verified)
        self.assertFalse(report.mexc_orders_sent)
        self.assertFalse(report.database_writes_sent)
        self.assertFalse(report.retry_allowed)
        self.assertFalse(report.live_authorization)
        self.assertFalse(report.entry_allowed)
        self.assertNotIn(orders[0]["symbol"], str(report.safe_summary()))
        self.assertNotIn(orders[0]["externalOid"], str(report.safe_summary()))

    async def test_absent_order_is_never_proof_post_was_absent(self):
        _, orders, _, _ = prepare_rows(1)
        client = MockMexcGetOnly([], [], [])
        report = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(
            report.status,
            "PENDING_ORIGIN_ALL_ABSENT_STILL_UNKNOWN_NO_RETRY",
        )
        self.assertEqual(report.origins_absent_unknown, 1)
        self.assertEqual(report.origins_checked, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(report.retry_allowed)

    async def test_mixed_origins_missing_and_present_are_non_authorizing(self):
        _, orders, _, _ = prepare_rows(2)
        client = MockMexcGetOnly([orders[0]], [], [])
        report = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(
            report.status, "PENDING_ORIGIN_MIXED_OBSERVATIONS_NO_RETRY",
        )
        self.assertEqual(report.origins_checked, 2)
        self.assertEqual(report.origin_orders_seen, 1)
        self.assertEqual(report.origins_absent_unknown, 1)
        self.assertEqual(len(client.calls), 4)
        self.assertFalse(report.entry_allowed)

    async def test_reserved_slot_has_no_order_origin_to_query(self):
        _, orders, _, _ = prepare_rows(1)
        client = MockMexcGetOnly(orders, [], [])
        report = await observe_pending_origins_get_only(
            triage=pending(orders, state="RESERVED"), client=client,
        )
        self.assertEqual(
            report.status,
            "PENDING_ORIGIN_RESERVED_ONLY_NO_EXCHANGE_INFERENCE",
        )
        self.assertEqual(report.reserved_unstaged, 1)
        self.assertEqual(client.calls, [])
        self.assertFalse(report.retry_allowed)

    async def test_any_invalid_origin_blocks_before_all_get(self):
        _, orders, _, _ = prepare_rows(2)
        base = pending(orders)
        client = MockMexcGetOnly(orders, [], [])
        invalid = replace(
            base, attempts=(
                base.attempts[0],
                replace(base.attempts[1], external_oid="../traversal"),
            ),
        )
        report = await observe_pending_origins_get_only(
            triage=invalid, client=client,
        )
        self.assertEqual(
            report.status, "PENDING_ORIGIN_IDENTITIES_INVALID_NO_RETRY",
        )
        self.assertEqual(client.calls, [])

    async def test_wrong_side_is_not_owned_short_order(self):
        _, orders, _, _ = prepare_rows(1)
        altered = [dict(orders[0], side=1)]
        client = MockMexcGetOnly(altered, [], [])
        report = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(
            report.status,
            "PENDING_ORIGIN_ORDER_SIDE_UNVERIFIED_NO_RETRY",
        )
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(report.order_fill_verified)

    async def test_two_order_bookends_change_during_recovery(self):
        _, orders, _, _ = prepare_rows(1)

        class Changing(MockMexcGetOnly):
            async def _private_get(self, path, params=None):
                result = await super()._private_get(path, params)
                if len(self.calls) == 2 and result is not None:
                    result["state"] = 999
                return result

        client = Changing(orders, [], [])
        result = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(
            result.status, "PENDING_ORIGIN_ORDER_BOOKENDS_CHANGED_NO_RETRY",
        )
        self.assertFalse(result.retry_allowed)
        self.assertFalse(result.entry_allowed)

    async def test_wrong_order_origin_or_failed_read_does_not_leak(self):
        _, orders, _, _ = prepare_rows(1)
        client = MockMexcGetOnly(orders, [], [])
        client.orders[(orders[0]["symbol"], orders[0]["externalOid"])][
            "externalOid"
        ] = "different-origin"
        r = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(r.status, "PENDING_ORIGIN_GET_FAILED_NO_RETRY")
        self.assertFalse(r.retry_allowed)
        client = MockMexcGetOnly(orders, [], [])
        client.fail_path = (
            f"/api/v1/private/order/external/"
            f"{orders[0]['symbol']}/{orders[0]['externalOid']}"
        )
        r = await observe_pending_origins_get_only(
            triage=pending(orders), client=client,
        )
        self.assertEqual(r.status, "PENDING_ORIGIN_GET_FAILED_NO_RETRY")
        self.assertNotIn(orders[0]["externalOid"], str(r.safe_summary()))

    async def test_missing_or_forged_triage_prevents_network_access(self):
        _, orders, _, _ = prepare_rows(1)
        client = MockMexcGetOnly(orders, [], [])
        for triage in (
            None,
            FourSlotRestartTriage(
                status="RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING",
            ),
            replace(pending(orders), retry_allowed=True),
            replace(pending(orders), pending_attempts=0),
        ):
            with self.subTest(triage=triage):
                r = await observe_pending_origins_get_only(
                    triage=triage, client=client,
                )
                self.assertEqual(
                    r.status, "PENDING_ORIGIN_TRIAGE_OR_CLIENT_INVALID_NO_RETRY",
                )
        self.assertEqual(client.calls, [])

    def test_no_order_or_database_mutation_function_in_module(self):
        import inspect
        import app.t100_live_four_slot_pending_origin_get as module
        src = inspect.getsource(module)
        for forbidden in (
            "._private_post(", "submit_short_with_stop(",
            ".persist_claimed(", ".claim(", ".execute(",
            "LiveMutationPermit", "trigger_deploy(",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
