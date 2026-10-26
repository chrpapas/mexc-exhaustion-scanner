"""Offline coherent current-inventory completeness probe tests."""
import unittest
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)


class Probe:
    def __init__(self):
        self.positions = [{
            "positionId": "10", "symbol": "SIREN_USDT",
            "positionType": 2, "state": 1, "holdVol": 5,
            "im": 10, "unRealizedPnl": 1,
        }]
        self.stops = [{
            "id": "99", "positionId": "10", "symbol": "SIREN_USDT",
            "positionType": 2, "state": 1, "isFinished": 0,
            "stopLossPrice": 175, "vol": 5,
        }]
        self.assets = [{
            "currency": "USDT", "positionMargin": 10, "unrealized": 1,
        }]
        self.counts = {
            "sumCount": 1, "limitOrderCount": 0,
            "stopOrderCount": 1, "planOrderCount": 0,
            "trackOrderCount": 0,
        }
        self.aggregate_position_reads = 0
        self.aggregate_stop_reads = 0

    async def raw_open_positions(self, symbol=None):
        if symbol is not None:
            return [x for x in self.positions if x["symbol"] == symbol]
        self.aggregate_position_reads += 1
        return [dict(x) for x in self.positions]

    async def raw_open_stops(self, symbol=None):
        if symbol is not None:
            return [x for x in self.stops if x["symbol"] == symbol]
        self.aggregate_stop_reads += 1
        return [dict(x) for x in self.stops]

    async def raw_account_assets(self):
        return [dict(x) for x in self.assets]

    async def raw_open_order_counts(self):
        return dict(self.counts)


class CompletenessProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonflat_coherent_window_verifies(self):
        r = await run_current_inventory_completeness_probe(Probe())
        self.assertTrue(r["account_wide_completeness_verified"])
        self.assertTrue(
            r["critical_inventory_equal_across_witness_window"]
        )
        self.assertFalse(r["exchange_atomicity_verified"])
        self.assertFalse(r["live_authorization"])

    async def test_stop_count_mismatch_blocks(self):
        p = Probe()
        p.counts["stopOrderCount"] = 0
        p.counts["sumCount"] = 0
        r = await run_current_inventory_completeness_probe(p)
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertEqual(r["status"], "CURRENT_TPSL_COUNT_MISMATCH")

    async def test_position_change_across_window_blocks(self):
        class Drifting(Probe):
            async def raw_open_positions(self, symbol=None):
                rows = await super().raw_open_positions(symbol)
                if symbol is None and self.aggregate_position_reads >= 2:
                    rows[0]["holdVol"] = 4
                return rows
        r = await run_current_inventory_completeness_probe(Drifting())
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertFalse(
            r["critical_positions_equal_across_witness_window"]
        )

    async def test_margin_mismatch_blocks(self):
        p = Probe()
        p.assets[0]["positionMargin"] = 11
        r = await run_current_inventory_completeness_probe(p)
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertEqual(r["status"], "POSITION_MARGIN_WITNESS_MISSING")

    async def test_symbol_filtered_mismatch_blocks(self):
        class BadFilter(Probe):
            async def raw_open_positions(self, symbol=None):
                if symbol is not None:
                    return []
                return await super().raw_open_positions(symbol)
        r = await run_current_inventory_completeness_probe(BadFilter())
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertEqual(
            r["status"], "SYMBOL_SCOPED_REDUNDANCY_MISMATCH"
        )

    async def test_flat_account_can_verify_with_zero_margin_and_counts(self):
        p = Probe()
        p.positions = []
        p.stops = []
        p.assets = [{
            "currency": "USDT", "positionMargin": 0, "unrealized": 0,
        }]
        p.counts = {
            "sumCount": 0, "limitOrderCount": 0,
            "stopOrderCount": 0, "planOrderCount": 0,
            "trackOrderCount": 0,
        }
        r = await run_current_inventory_completeness_probe(p)
        self.assertTrue(r["account_wide_completeness_verified"])

    async def test_transport_failure_is_contained(self):
        class Failed(Probe):
            async def raw_account_assets(self):
                raise TimeoutError()
        r = await run_current_inventory_completeness_probe(Failed())
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertEqual(
            r["status"], "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED"
        )

    async def test_no_private_values_in_certificate(self):
        p = Probe()
        p.positions[0]["symbol"] = "SECRET_USDT"
        p.stops[0]["symbol"] = "SECRET_USDT"
        r = await run_current_inventory_completeness_probe(p)
        cert = str(r["current_inventory_completeness"])
        for secret in ("SECRET_USDT", "10", "99", "175"):
            self.assertNotIn(secret, cert)


if __name__ == "__main__":
    unittest.main()
