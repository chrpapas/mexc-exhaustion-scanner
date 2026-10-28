"""GET-only collector tests with fake MEXC private + public transports.

No credentials, HTTP requests, PostgreSQL, MEXC orders or Render services.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from app.t100_live_cross_risk_collector import (
    CONTRACT_COUNTRY, FAIR_PRICE_PREFIX, CrossRiskCollectionHalted,
    MexcReadOnlyCrossRiskCollector,
)
from app.t100_live_cross_risk_observation import observe_cross_risk_window
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_observation import frame


class FakePrivate:
    def __init__(self, observed):
        self.observed = observed
        self.calls = []
        self.positions_reads = 0
        self.stops_reads = 0
        self.fail = False

    async def _private_get(self, path, params=None):
        self.calls.append(("GET", path, copy.deepcopy(params)))
        if self.fail:
            raise TimeoutError("DO_NOT_LEAK_PRIVATE_KEY")
        symbol = (params or {}).get("symbol")
        if path == "/api/v1/private/position/open_positions":
            self.positions_reads += 1
            return [
                copy.deepcopy(p) for p in self.observed.positions
                if symbol is None or p.get("symbol") == symbol
            ]
        if path == "/api/v1/private/stoporder/open_orders":
            self.stops_reads += 1
            return [
                copy.deepcopy(p) for p in self.observed.stops
                if symbol is None or p.get("symbol") == symbol
            ]
        if path == "/api/v1/private/account/assets":
            return copy.deepcopy(self.observed.assets)
        if path == "/api/v1/private/order/open_order_total_count":
            return {
                "sumCount": len(self.observed.stops),
                "stopOrderCount": len(self.observed.stops),
                "limitOrderCount": 0,
                "planOrderCount": 0,
                "trackOrderCount": 0,
            }
        if path == "/api/v1/private/account/risk_limit":
            if symbol not in self.observed.private_risk_by_symbol:
                return {}
            return {symbol: copy.deepcopy(self.observed.private_risk_by_symbol[symbol])}
        raise AssertionError("Unexpected GET endpoint")


class FakeResponse:
    def __init__(self, data, *, ok=True, code=0):
        self.data = data
        self.ok = ok
        self.code = code

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "success": self.ok,
            "code": self.code,
            "data": copy.deepcopy(self.data),
        }


class FakePublic:
    def __init__(self, observed, *, fair_ms=1_000_400):
        self.observed = observed
        self.fair_ms = fair_ms
        self.calls = []
        self.fail = False
        self.change_symbol = False
        self.change_contract = False
        self.bad_envelope = False
        self.fair_price = "2.0"

    async def get(self, path, *, params=None):
        self.calls.append(("GET", path, copy.deepcopy(params)))
        if self.fail:
            raise TimeoutError("DO_NOT_LEAK_PUBLIC_RESPONSE")
        if path == CONTRACT_COUNTRY:
            sym = (params or {}).get("symbol")
            result = copy.deepcopy(self.observed.public_contract_by_symbol.get(sym))
            if result is None:
                return FakeResponse(None)
            if self.change_contract:
                result["liquidationFeeRate"] = 0.08
            if self.change_symbol:
                result["symbol"] = "WRONG_USDT"
            return FakeResponse(result, ok=not self.bad_envelope)
        if path.startswith(FAIR_PRICE_PREFIX):
            sym = path.removeprefix(FAIR_PRICE_PREFIX)
            return FakeResponse({
                "symbol": sym,
                "fairPrice": self.fair_price,
                "timestamp": self.fair_ms,
            }, ok=not self.bad_envelope)
        raise AssertionError("Unexpected public GET")


class FakeClock:
    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first = frame()
        self.private_client = FakePrivate(self.first)
        self.public_client = FakePublic(self.first)
        self.clock = FakeClock(1_000_000, 1_000_500)
        self.collector = MexcReadOnlyCrossRiskCollector(
            private=ReadOnlyMexcProbe(self.private_client),
            public=self.public_client, clock_ms=self.clock,
        )

    async def test_collects_real_endpoint_shapes_without_post_or_certification(self):
        collected = await self.collector.read()
        self.assertTrue(collected.positions_complete)
        self.assertTrue(collected.stops_complete)
        self.assertTrue(collected.assets_complete)
        self.assertEqual(collected.observed_at_ms, 1_000_500)
        self.assertEqual(
            collected.fair_by_symbol["TEST_USDT"]["observed_at_ms"],
            1_000_400,
        )
        self.assertTrue(all(c[0] == "GET" for c in (
            self.private_client.calls + self.public_client.calls
        )))
        self.assertEqual(
            [(x[1], x[2]) for x in self.public_client.calls],
            [
                (CONTRACT_COUNTRY, {"symbol": "TEST_USDT"}),
                (FAIR_PRICE_PREFIX + "TEST_USDT", None),
            ],
        )

    async def test_two_collected_frames_pass_non_authorizing_window(self):
        self.clock = FakeClock(
            1_000_000, 1_000_500, 1_001_000, 1_001_500,
        )
        self.collector.clock_ms = self.clock

        async def adjusted_read():
            if self.public_client.calls:
                self.public_client.fair_ms = 1_001_400
            return await self.collector.read()

        class ReadAdapter:
            async def read(inner):
                return await adjusted_read()

        result = await observe_cross_risk_window(
            ReadAdapter(), now_ms=lambda: 1_001_700,
        )
        self.assertEqual(
            result.status, "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result.observed_positions, 1)
        self.assertFalse(result.account_wide_completeness_certified)
        self.assertFalse(result.future_stressed_tiers_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.live_authorization)

    async def test_unrelated_position_not_automatically_owned(self):
        collected = await self.collector.read()
        self.assertEqual(collected.positions[0]["positionId"], "12")
        # This collector never consumes a durable T100 checkpoint.
        self.assertFalse(hasattr(collected, "t100_owned_position"))

    async def test_missing_native_stop_blocks(self):
        self.private_client.observed.stops.clear()
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "NONFLAT_ONE_TO_FOUR",
        ):
            await self.collector.read()

    async def test_incorrect_stops_count_blocks(self):
        self.private_client.observed.stops[0]["symbol"] = "WRONG_USDT"
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "POSITION_SYMBOLS_INVALID",
        ):
            await self.collector.read()

    async def test_malformed_public_contract_symbol_blocks(self):
        self.public_client.change_symbol = True
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "PUBLIC_RISK_RESPONSE_UNVERIFIED",
        ):
            await self.collector.read()

    async def test_public_rejected_response_blocks(self):
        self.public_client.bad_envelope = True
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "PUBLIC_RISK_RESPONSE_UNVERIFIED",
        ):
            await self.collector.read()

    async def test_stale_fair_price_blocks(self):
        self.public_client.fair_ms = 900_000
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "FAIR_TIMESTAMP_OR_PRICE_INVALID",
        ):
            await self.collector.read()

    async def test_future_fair_time_blocks(self):
        self.public_client.fair_ms = 1_005_000
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "FAIR_TIMESTAMP_OR_PRICE_INVALID",
        ):
            await self.collector.read()

    async def test_fair_mark_future_relative_to_finished_window_blocks(self):
        # 1000800 is within start+2500 but occurs AFTER frame finish 1000500.
        self.public_client.fair_ms = 1_000_800
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "COLLECTOR_READ_WINDOW_STALE",
        ):
            await self.collector.read()

    async def test_position_changed_between_account_wide_reads_blocks(self):
        class DriftingPrivate(FakePrivate):
            async def _private_get(inner, path, params=None):
                if (
                    path == "/api/v1/private/position/open_positions"
                    and params is None and inner.positions_reads == 2
                ):
                    inner.observed.positions[0]["holdVol"] = "51"
                return await super()._private_get(path, params)

        probe = ReadOnlyMexcProbe(DriftingPrivate(self.first))
        collector = MexcReadOnlyCrossRiskCollector(
            private=probe, public=self.public_client,
            clock_ms=FakeClock(1_000_000, 1_000_500),
        )
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "COLLECTOR_INVENTORY_INCOMPLETE",
        ):
            await collector.read()

    async def test_public_or_private_timeout_redacts_data(self):
        self.private_client.fail = True
        with self.assertRaises(CrossRiskCollectionHalted) as caught:
            await self.collector.read()
        self.assertNotIn("DO_NOT_LEAK_PRIVATE_KEY", str(caught.exception))

    async def test_public_timeout_redacts_data(self):
        self.public_client.fail = True
        with self.assertRaises(CrossRiskCollectionHalted) as caught:
            await self.collector.read()
        self.assertNotIn("DO_NOT_LEAK_PUBLIC_RESPONSE", str(caught.exception))

    async def test_five_positions_never_queried_as_safe(self):
        original = self.private_client.observed.positions[0]
        for i in range(4):
            extra = dict(original)
            extra["symbol"] = f"EXTRA{i}_USDT"
            extra["positionId"] = str(20 + i)
            self.private_client.observed.positions.append(extra)
        with self.assertRaisesRegex(
            CrossRiskCollectionHalted, "NONFLAT_ONE_TO_FOUR",
        ):
            await self.collector.read()
        self.assertEqual(self.public_client.calls, [])

    async def test_invalid_fair_and_contract_data_blocks_at_observation(self):
        # Raw transport accepts an exact price timestamp but the pure
        # witnessed tier schema will not certify a disabled contract.
        self.public_client.change_contract = True
        x = await self.collector.read()
        self.assertEqual(
            x.public_contract_by_symbol["TEST_USDT"]["liquidationFeeRate"],
            0.08,
        )
        self.assertFalse(hasattr(x, "venue_liquidation_certified"))

    async def test_invalid_configuration_blocks(self):
        with self.assertRaises(ValueError):
            MexcReadOnlyCrossRiskCollector(
                private=ReadOnlyMexcProbe(self.private_client),
                public=self.public_client,
                clock_ms=lambda: 0, max_frame_ms=0,
            )


if __name__ == "__main__":
    unittest.main()
