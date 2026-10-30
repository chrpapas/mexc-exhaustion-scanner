"""T100 V40 real-contract MEXC GET-only four-slot integration regressions.

All transport is an in-memory private GET mock. The test makes no live
calls, never knows account credentials and does not import an order adapter.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal as D
import unittest

from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue, FourSlotMexcCompleteness,
    inspect_existing_four_slots_get_only,
    inspect_staged_four_slot_fill_get_only,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Ledger, fixture,
)
from tests.test_t100_live_four_slot_attempt_outcome import ProtectedOutcomeTests


def _wire(value):
    """Simulate decoded signed-GET JSON, never Python Decimal in wire data."""
    return json.loads(json.dumps(value, default=str))


class MockMexcGetOnly:
    def __init__(self, orders, positions, stops):
        self.positions = copy.deepcopy(positions)
        self.stops = copy.deepcopy(stops)
        self.orders = {
            (r["symbol"], r["externalOid"]): copy.deepcopy(r) for r in orders
        }
        self.calls = []
        self.fail_path = None
        self.fail_filtered_stop_symbol = None
        self.stop_count_override = None
        self.assets_override = None

    async def _private_get(self, path, params=None):
        self.calls.append((path, copy.deepcopy(params)))
        if path == self.fail_path:
            raise TimeoutError("secret details not emitted")
        symbol = (params or {}).get("symbol")
        if path == "/api/v1/private/position/open_positions":
            return [_wire(p) for p in self.positions
                    if symbol is None or p["symbol"] == symbol]
        if path == "/api/v1/private/stoporder/open_orders":
            if symbol == self.fail_filtered_stop_symbol:
                return []
            return [_wire(s) for s in self.stops
                    if symbol is None or s["symbol"] == symbol]
        if path == "/api/v1/private/account/assets":
            if self.assets_override is not None:
                return _wire(self.assets_override)
            return _wire([{
                "currency": "USDT",
                "positionMargin": sum(D(str(p.get("im", 0))) for p in self.positions),
                "unrealized": sum(D(str(p.get("unRealizedPnl", 0))) for p in self.positions),
            }])
        if path == "/api/v1/private/order/open_order_total_count":
            n = len(self.stops) if self.stop_count_override is None else self.stop_count_override
            return {
                "sumCount": n, "limitOrderCount": 0,
                "stopOrderCount": n, "planOrderCount": 0,
                "trackOrderCount": 0,
            }
        for (sym, oid), order in self.orders.items():
            if path == f"/api/v1/private/order/external/{sym}/{oid}":
                return _wire(order)
        if path.startswith("/api/v1/private/order/external/"):
            return None
        raise AssertionError("unexpected exchange endpoint")

    async def _private_post(self, *a, **kw):
        raise AssertionError("REAL ORDER POST MUST NEVER BE CALLED")


def prepare_rows(n):
    snap, orders, positions, stops = fixture(n)
    for p in positions:
        p.update({"im": D("10"), "unRealizedPnl": D("0"), "openType": 2})
    for s in stops:
        s.update({"isFinished": 0, "positionType": 2})
    return snap, orders, positions, stops


class ReadOnlyFourSlotGetTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_owned_slots_recovered_via_real_get_probe_and_bookends(self):
        snap, orders, positions, stops = prepare_rows(3)
        client = MockMexcGetOnly(orders, positions, stops)
        result = await inspect_existing_four_slots_get_only(
            ledger=Ledger(snap), client=client,
        )
        self.assertEqual(result.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING")
        self.assertEqual(result.occupied_slots, 3)
        self.assertTrue(result.inventory["account_wide_completeness_verified"])
        self.assertEqual(len(result.recovery.recovered), 3)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertGreaterEqual(
            sum("/private/order/external/" in path
                for path, _ in client.calls), 3,
        )
        self.assertTrue(all("/private/" in path for path, _ in client.calls))

    async def test_flat_clean_research_observation_is_not_entry_authorization(self):
        snap, orders, positions, stops = prepare_rows(0)
        client = MockMexcGetOnly(orders, positions, stops)
        result = await inspect_existing_four_slots_get_only(
            ledger=Ledger(snap), client=client,
        )
        self.assertEqual(result.status, "FLAT_OBSERVED_NONAUTHORIZING")
        self.assertTrue(result.inventory["account_wide_completeness_verified"])
        self.assertFalse(result.entry_allowed)

    async def test_manual_unowned_position_never_adopted(self):
        snap, _, _, _ = prepare_rows(0)
        _, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        result = await inspect_existing_four_slots_get_only(
            ledger=Ledger(snap), client=client,
        )
        self.assertEqual(result.status, "UNOWNED_OR_MISSING_CHECKPOINTS")
        self.assertFalse(result.entry_allowed)

    async def test_stale_symbol_scoped_or_count_witness_fails_closed(self):
        snap, orders, positions, stops = prepare_rows(1)
        for change in ("filtered_stop", "count", "margin", "get_timeout"):
            with self.subTest(change=change):
                client = MockMexcGetOnly(orders, positions, stops)
                if change == "filtered_stop":
                    client.fail_filtered_stop_symbol = positions[0]["symbol"]
                elif change == "count":
                    client.stop_count_override = 0
                elif change == "margin":
                    client.assets_override = [{
                        "currency": "USDT", "positionMargin": 99,
                        "unrealized": 0,
                    }]
                else:
                    client.fail_path = "/api/v1/private/account/assets"
                result = await inspect_existing_four_slots_get_only(
                    ledger=Ledger(snap), client=client,
                )
                self.assertNotEqual(
                    result.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
                )
                self.assertFalse(result.entry_allowed)

    async def test_exact_oid_get_validates_paths_and_response_identity(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        probe = ReadOnlyMexcProbe(client)
        for symbol, oid in (
            ("SIREN/USDT", "t100v1eabc"),
            ("SIREN_USDT", "../escape"),
            ("SIREN_USDT", "with space"),
            ("SIREN_USDT", ""),
        ):
            with self.subTest(symbol=symbol), self.assertRaises(VenueReadHalted):
                await probe.raw_order_by_external_oid(symbol, oid)
        self.assertEqual(len(client.calls), 0)
        original = orders[0]
        self.assertEqual(
            (await probe.raw_order_by_external_oid(
                original["symbol"], original["externalOid"]
            ))["orderId"],
            original["orderId"],
        )
        # A mismatched payload is UNKNOWN, not proof of no order.
        client.orders[(original["symbol"], original["externalOid"])]["symbol"] = "OTHER_USDT"
        with self.assertRaisesRegex(VenueReadHalted, "IDENTITY_MISMATCH"):
            await probe.raw_order_by_external_oid(
                original["symbol"], original["externalOid"]
            )

    async def test_staged_fourth_protected_fill_uses_only_gets(self):
        existing = ProtectedOutcomeTests()
        await existing.asyncSetUp()
        venue = existing.venue
        positions = copy.deepcopy(venue.first_positions)
        stops = copy.deepcopy(venue.first_stops)
        for p in positions:
            p.update({"im": D("10"), "unRealizedPnl": D("0")})
        new = venue.orders[("FOURTH_USDT", existing.intent.external_oid)]
        client = MockMexcGetOnly([new], positions, stops)
        result = await inspect_staged_four_slot_fill_get_only(
            stages=[existing.stage],
            intents=[existing.intent],
            pre_attempt_witness=existing.before,
            client=client,
        )
        self.assertEqual(
            result.status,
            "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result.newly_observed_protected_positions, 1)
        self.assertTrue(result.all_staged_attempts_fully_filled_and_protected)
        self.assertFalse(result.durable_ack_persisted)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)

    async def test_staged_missing_order_or_stop_blocks_without_post_or_retry(self):
        existing = ProtectedOutcomeTests()
        await existing.asyncSetUp()
        positions = copy.deepcopy(existing.venue.first_positions)
        stops = copy.deepcopy(existing.venue.first_stops)
        for p in positions:
            p.update({"im": D("10"), "unRealizedPnl": D("0")})
        new = existing.venue.orders[("FOURTH_USDT", existing.intent.external_oid)]
        for mode in ("missing_order", "short_stop", "filtered_mismatch"):
            client = MockMexcGetOnly(
                [] if mode == "missing_order" else [new],
                positions, stops,
            )
            if mode == "short_stop":
                client.stops[-1]["vol"] = 1
            if mode == "filtered_mismatch":
                client.fail_filtered_stop_symbol = "FOURTH_USDT"
            result = await inspect_staged_four_slot_fill_get_only(
                stages=[existing.stage], intents=[existing.intent],
                pre_attempt_witness=existing.before, client=client,
            )
            self.assertFalse(result.live_authorization)
            self.assertFalse(result.all_staged_attempts_fully_filled_and_protected)
            self.assertFalse(result.mexc_orders_sent)
            self.assertTrue(all("/private/" in path for path, _ in client.calls))

    def test_integration_has_no_live_order_adapter_or_db_writer(self):
        import inspect
        import app.t100_live_four_slot_mexc_readonly as module
        s = inspect.getsource(module)
        for forbidden in (
            "LiveMexcAdapter", "submit_short_with_stop(",
            "PostgresProtectedV40Commit(", "._private_post(",
            "LiveMutationPermit", "httpx.", "asyncpg.",
        ):
            self.assertNotIn(forbidden, s)


if __name__ == "__main__":
    unittest.main()
