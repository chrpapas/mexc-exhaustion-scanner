"""GET-only inventory shape tests: flat != failed risk witness.

All mock data is fictional. No network, credentials or MEXC mutations.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from app.t100_live_cross_risk_inventory_shape import observe_inventory_shape
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import FakePrivate
from tests.test_t100_live_cross_risk_observation import frame


class ShapeTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, data):
        client = FakePrivate(data)
        result = await observe_inventory_shape(ReadOnlyMexcProbe(client))
        self.assertTrue(all(x[0] == "GET" for x in client.calls))
        self.assertFalse(result.account_wide_completeness_certified)
        self.assertFalse(result.protected_position_ownership_verified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertNotIn("TEST_USDT", str(result))
        self.assertNotIn("positionId", str(result))
        return result

    async def test_flat_and_order_free_is_observed_but_not_authorized(self):
        sample = frame()
        sample.positions.clear()
        sample.stops.clear()
        result = await self._probe(sample)
        self.assertEqual(
            result.status, "INVENTORY_SHAPE_FLAT_AND_ORDER_FREE_OBSERVED_NONATOMIC",
        )
        self.assertTrue(result.stable_across_reads)
        self.assertEqual(result.observed_positions, 0)
        self.assertEqual(result.observed_current_stops, 0)
        self.assertEqual(result.reported_total_open_orders, 0)
        self.assertTrue(result.order_counts_match)

    async def test_one_position_and_one_stop(self):
        result = await self._probe(frame())
        self.assertEqual(
            result.status, "INVENTORY_SHAPE_NONFLAT_COUNTS_MATCH_NONAUTHORIZING",
        )
        self.assertEqual(result.observed_positions, 1)
        self.assertEqual(result.observed_current_stops, 1)

    async def test_unmatched_stops_count_distinguished(self):
        sample = frame()
        sample.stops.clear()
        result = await self._probe(sample)
        self.assertEqual(
            result.status, "INVENTORY_SHAPE_NONFLAT_STOP_ROW_COUNT_DIFFERS",
        )
        self.assertEqual(result.observed_positions, 1)
        self.assertEqual(result.observed_current_stops, 0)

    async def test_flat_with_current_stops_distinguished(self):
        sample = frame()
        sample.positions.clear()
        result = await self._probe(sample)
        self.assertEqual(
            result.status, "INVENTORY_SHAPE_STOPS_WITHOUT_POSITION",
        )
        self.assertEqual(result.observed_positions, 0)
        self.assertEqual(result.observed_current_stops, 1)

    async def test_more_than_four_positions_distinguished(self):
        sample = frame()
        for i in range(4):
            p = dict(sample.positions[0], symbol=f"P{i}_USDT",
                     positionId=str(100 + i))
            sample.positions.append(p)
        result = await self._probe(sample)
        self.assertEqual(
            result.status, "INVENTORY_SHAPE_MORE_THAN_FOUR_POSITIONS",
        )
        self.assertEqual(result.observed_positions, 5)

    async def test_open_orders_with_flat_positions_not_called_order_free(self):
        sample = frame()
        sample.positions.clear()
        sample.stops.clear()

        class ExtraOrders(FakePrivate):
            async def _private_get(self, path, params=None):
                if path.endswith("open_order_total_count"):
                    self.calls.append(("GET", path, params))
                    return {
                        "sumCount": 1, "stopOrderCount": 0,
                        "limitOrderCount": 1, "planOrderCount": 0,
                        "trackOrderCount": 0,
                    }
                return await super()._private_get(path, params)

        observed = await observe_inventory_shape(ReadOnlyMexcProbe(ExtraOrders(sample)))
        self.assertEqual(observed.status, "INVENTORY_SHAPE_FLAT_WITH_OPEN_ORDERS")
        self.assertEqual(observed.reported_total_open_orders, 1)
        self.assertFalse(observed.entry_allowed)

    async def test_stop_count_endpoint_disagreement_is_not_protection(self):
        class InconsistentCount(FakePrivate):
            async def _private_get(self, path, params=None):
                if path.endswith("open_order_total_count"):
                    self.calls.append(("GET", path, params))
                    return {
                        "sumCount": 0, "stopOrderCount": 0,
                        "limitOrderCount": 0, "planOrderCount": 0,
                        "trackOrderCount": 0,
                    }
                return await super()._private_get(path, params)

        observed = await observe_inventory_shape(
            ReadOnlyMexcProbe(InconsistentCount(frame()))
        )
        self.assertEqual(
            observed.status, "INVENTORY_SHAPE_STOP_COUNT_DISAGREES_WITH_EXCHANGE",
        )
        self.assertFalse(observed.order_counts_match)
        self.assertEqual(observed.observed_current_stops, 1)

    async def test_mid_read_inventory_drift_never_reports_counts_as_verified(self):
        class MidReadChanged(FakePrivate):
            async def _private_get(self, path, params=None):
                if path.endswith("open_positions") and params is None and self.positions_reads == 1:
                    self.observed.positions[0]["holdVol"] = "55"
                return await super()._private_get(path, params)
        result = await observe_inventory_shape(
            ReadOnlyMexcProbe(MidReadChanged(frame()))
        )
        self.assertEqual(result.status, "INVENTORY_SHAPE_ACCOUNT_STATE_DRIFT")
        self.assertIsNone(result.observed_positions)
        self.assertFalse(result.stable_across_reads)

    async def test_failed_signed_get_never_prints_secret(self):
        client = FakePrivate(frame())
        client.fail = True
        result = await observe_inventory_shape(ReadOnlyMexcProbe(client))
        self.assertEqual(result.status, "INVENTORY_SHAPE_READ_UNVERIFIED")
        self.assertIsNone(result.observed_positions)
        self.assertNotIn("DO_NOT_LEAK_PRIVATE_KEY", str(result))


if __name__ == "__main__":
    unittest.main()
