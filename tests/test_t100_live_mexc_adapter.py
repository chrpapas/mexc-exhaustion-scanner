"""Offline current MEXC live-adapter contract tests. No HTTP is sent."""
import unittest
from decimal import Decimal as D

from app.t100_live_authorization import (
    LiveAuthorizationHalted,
    LivePilotReadinessEvidence,
    issue_live_mutation_permit,
)
from app.t100_live_execution import EntryIntent
from app.t100_live_guards import LIVE_CONFIRMATION, T100ExecutionControls
from app.t100_live_mexc_adapter import LiveMexcAdapter


class FakeClient:
    def __init__(self):
        self.posts = []
        self.open_stop_calls = []
        self.post_result = None
        self.post_error = None
        self.current_stops = []
        self.get_results = {}
        self.get_calls = []

    async def _private_post(self, path, body):
        self.posts.append((path, body))
        if self.post_error is not None:
            raise self.post_error
        return self.post_result

    async def _private_get(self, path, params=None):
        self.get_calls.append((path, params))
        if path not in self.get_results:
            raise AssertionError("unexpected _private_get")
        return self.get_results[path]

    async def open_stop_orders(self, symbol=None):
        self.open_stop_calls.append(symbol)
        return [dict(x) for x in self.current_stops]


def permit(max_position="1000"):
    controls = T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE": "live",
        "T100_LIVE_ORDER_API_ENABLED": "true",
        "T100_LIVE_KILL_SWITCH": "false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": max_position,
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "1000",
        "T100_LIVE_MAX_DAILY_LOSS_USDT": "25",
        "T100_LIVE_LEVERAGE": "1",
    })
    evidence = LivePilotReadinessEvidence(
        code_revision_verified=True,
        durable_schema_deployed_verified=True,
        current_inventory_completeness_verified=True,
        account_reconciled=True,
        protected_position_soak_verified=True,
        restart_recovery_verified=True,
        native_stop_mutation_contract_verified=True,
        entry_reconciliation_verified=True,
        fill_and_accounting_verified=True,
        private_ws_lifecycle_semantics_verified=True,
        unresolved_mutations_absent=True,
        independent_review_completed=True,
        operator_tiny_pilot_authorized=True,
    )
    return issue_live_mutation_permit(controls, evidence)


def intent():
    return EntryIntent(
        signal_id=1,
        symbol="SIREN_USDT",
        reference_price=D("100"),
        notional_usdt=D("500"),
        volume_contracts=D("5"),
        stop_price=D("175"),
        external_oid="t100v1eabc",
    )


class LiveMexcAdapterContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.adapter = LiveMexcAdapter(self.client)

    async def test_order_deal_details_uses_immutable_order_endpoint(self):
        self.client.get_results[
            "/api/v1/private/order/deal_details/100"
        ] = [{"id": "f1", "orderId": "100"}]
        rows = await self.adapter.order_deal_details("100")
        self.assertEqual(rows, [{"id": "f1", "orderId": "100"}])
        self.assertEqual(
            self.client.get_calls,
            [("/api/v1/private/order/deal_details/100", None)],
        )

    async def test_order_deal_details_fails_closed_on_bad_shape(self):
        for bad in (None, {}, [None]):
            with self.subTest(bad=bad):
                self.client.get_results[
                    "/api/v1/private/order/deal_details/100"
                ] = bad
                with self.assertRaisesRegex(
                    RuntimeError, "DEAL_DETAILS_UNVERIFIED"
                ):
                    await self.adapter.order_deal_details("100")

    async def test_order_deal_details_rejects_invalid_order_id_before_get(self):
        for bad in ("", "abc", "0", "-1"):
            with self.subTest(bad=bad):
                self.client.get_calls = []
                with self.assertRaises(ValueError):
                    await self.adapter.order_deal_details(bad)
                self.assertEqual(self.client.get_calls, [])

    async def test_mutation_without_valid_permit_never_posts(self):
        with self.assertRaises(LiveAuthorizationHalted):
            await self.adapter.submit_short_with_stop(
                intent(), permit=None
            )
        self.assertEqual(self.client.posts, [])

        with self.assertRaises(LiveAuthorizationHalted):
            await self.adapter.submit_native_short_stop_tightening(
                stop_order_id=99,
                previous_stop_price="175",
                new_stop_price="90",
                permit=None,
            )
        self.assertEqual(self.client.posts, [])

    async def test_entry_uses_current_place_order_contract(self):
        self.client.post_result = {
            "orderId": "739113577038255616",
            "ts": 1761888808839,
        }
        order_id = await self.adapter.submit_short_with_stop(
            intent(), permit=permit()
        )
        self.assertEqual(order_id, "739113577038255616")
        self.assertEqual(len(self.client.posts), 1)
        path, body = self.client.posts[0]
        self.assertEqual(path, "/api/v1/private/order/create")
        self.assertEqual(body["symbol"], "SIREN_USDT")
        self.assertEqual(body["side"], 3)
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["openType"], 1)
        self.assertEqual(body["positionMode"], 1)
        self.assertEqual(body["externalOid"], "t100v1eabc")
        self.assertEqual(body["leverage"], 1)
        self.assertEqual(body["stopLossPrice"], "175")
        self.assertEqual(body["lossTrend"], 2)

    async def test_entry_requires_order_id_in_unwrapped_data(self):
        for bad in (None, {}, {"ts": 1}, "123"):
            with self.subTest(bad=bad):
                self.client.posts = []
                self.client.post_result = bad
                with self.assertRaisesRegex(
                    RuntimeError, "did not return orderId"
                ):
                    await self.adapter.submit_short_with_stop(
                        intent(), permit=permit()
                    )

    async def test_one_shot_stop_tightening_contract(self):
        await self.adapter.submit_native_short_stop_tightening(
            stop_order_id=99,
            previous_stop_price="175",
            new_stop_price="90",
            permit=permit(),
        )
        self.assertEqual(
            self.client.posts,
            [(
                "/api/v1/private/stoporder/change_plan_price",
                {
                    "stopPlanOrderId": 99,
                    "lossTrend": 2,
                    "stopLossPrice": "90",
                },
            )],
        )

    async def test_stop_tightening_never_retries_ambiguous_post(self):
        self.client.post_error = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            await self.adapter.submit_native_short_stop_tightening(
                stop_order_id=99,
                previous_stop_price="175",
                new_stop_price="90",
                permit=permit(),
            )
        self.assertEqual(len(self.client.posts), 1)

    async def test_non_tightening_or_invalid_stop_never_posts(self):
        cases = (
            (99, "175", "175"),
            (99, "175", "176"),
            (99, "175", "0"),
            (0, "175", "90"),
            (True, "175", "90"),
        )
        for stop_id, previous, requested in cases:
            with self.subTest(
                stop_id=stop_id, previous=previous, requested=requested
            ):
                self.client.posts = []
                with self.assertRaises(ValueError):
                    await self.adapter.submit_native_short_stop_tightening(
                        stop_order_id=stop_id,
                        previous_stop_price=previous,
                        new_stop_price=requested,
                        permit=permit(),
                    )
                self.assertEqual(self.client.posts, [])

    async def test_open_stops_uses_dedicated_current_endpoint_wrapper(self):
        self.client.current_stops = [{
            "id": 99,
            "positionId": 10,
            "symbol": "SIREN_USDT",
            "state": 1,
        }]
        rows = await self.adapter.open_stop_orders("SIREN_USDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.client.open_stop_calls, ["SIREN_USDT"])

    async def test_legacy_send_then_read_verifies_without_second_post(self):
        self.client.current_stops = [{
            "id": 99,
            "positionId": 10,
            "symbol": "SIREN_USDT",
            "state": 1,
            "stopLossPrice": "89",
        }]
        await self.adapter.tighten_native_short_stop(
            symbol="SIREN_USDT",
            stop_order_id=99,
            position_id=10,
            previous_stop_price="175",
            new_stop_price="90",
            permit=permit(),
        )
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.open_stop_calls, ["SIREN_USDT"])

    async def test_legacy_send_then_read_fails_if_current_stop_not_verified(self):
        self.client.current_stops = []
        with self.assertRaisesRegex(
            RuntimeError, "NATIVE_STOP_MODIFICATION_UNVERIFIED"
        ):
            await self.adapter.tighten_native_short_stop(
                symbol="SIREN_USDT",
                stop_order_id=99,
                position_id=10,
                previous_stop_price="175",
                new_stop_price="90",
                permit=permit(),
            )
        self.assertEqual(len(self.client.posts), 1)


if __name__ == "__main__":
    unittest.main()
