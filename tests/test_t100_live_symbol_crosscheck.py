"""Offline tests for symbol-scoped MEXC inventory redundancy checks."""
import unittest

from app.t100_live_symbol_crosscheck import crosscheck_symbol_scoped_inventory


def pos(symbol="SIREN_USDT", vol="20", pnl="1"):
    return {
        "symbol": symbol,
        "positionId": "10",
        "positionType": 2,
        "state": 1,
        "holdVol": vol,
        "holdAvgPrice": "100",
        "unRealizedPnl": pnl,
    }


def stop(symbol="SIREN_USDT", price="175"):
    return {
        "symbol": symbol,
        "positionId": "10",
        "id": "99",
        "state": 1,
        "stopLossPrice": price,
        "vol": "20",
    }


class Probe:
    def __init__(self, positions_by_symbol, stops_by_symbol):
        self.positions_by_symbol = positions_by_symbol
        self.stops_by_symbol = stops_by_symbol
        self.calls = []

    async def raw_open_positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        return self.positions_by_symbol.get(symbol, [])

    async def raw_open_stops(self, symbol=None):
        self.calls.append(("stops", symbol))
        return self.stops_by_symbol.get(symbol, [])


class SymbolCrosscheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_position_without_current_tpsl(self):
        aggregate_positions = [pos()]
        aggregate_stops = []
        probe = Probe({"SIREN_USDT": [pos(pnl="9")]}, {"SIREN_USDT": []})
        r = await crosscheck_symbol_scoped_inventory(
            probe, aggregate_positions, aggregate_stops)
        self.assertTrue(r["position_symbol_scoped_match"])
        self.assertTrue(r["current_tpsl_symbol_scoped_match"])
        self.assertTrue(r["symbol_scoped_crosscheck_verified"])
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertFalse(r["exchange_atomicity_verified"])
        self.assertEqual(r["symbols_checked"], 1)
        self.assertEqual(
            probe.calls,
            [("positions", "SIREN_USDT"), ("stops", "SIREN_USDT")],
        )

    async def test_volatile_position_field_does_not_break_match(self):
        aggregate_positions = [pos(pnl="1")]
        probe = Probe({"SIREN_USDT": [pos(pnl="999")]}, {"SIREN_USDT": []})
        r = await crosscheck_symbol_scoped_inventory(
            probe, aggregate_positions, [])
        self.assertTrue(r["symbol_scoped_crosscheck_verified"])

    async def test_critical_position_quantity_mismatch_detected(self):
        aggregate_positions = [pos(vol="20")]
        probe = Probe({"SIREN_USDT": [pos(vol="19")]}, {"SIREN_USDT": []})
        r = await crosscheck_symbol_scoped_inventory(
            probe, aggregate_positions, [])
        self.assertFalse(r["position_symbol_scoped_match"])
        self.assertEqual(r["status"], "SYMBOL_SCOPED_REDUNDANCY_MISMATCH")

    async def test_stop_price_mismatch_detected(self):
        aggregate_positions = [pos()]
        aggregate_stops = [stop(price="175")]
        probe = Probe(
            {"SIREN_USDT": [pos()]},
            {"SIREN_USDT": [stop(price="174")]},
        )
        r = await crosscheck_symbol_scoped_inventory(
            probe, aggregate_positions, aggregate_stops)
        self.assertFalse(r["current_tpsl_symbol_scoped_match"])

    async def test_filtered_wrong_symbol_rejected(self):
        aggregate_positions = [pos()]
        probe = Probe(
            {"SIREN_USDT": [pos(symbol="OTHER_USDT")]},
            {"SIREN_USDT": []},
        )
        r = await crosscheck_symbol_scoped_inventory(
            probe, aggregate_positions, [])
        self.assertEqual(r["status"], "FILTERED_POSITION_SYMBOL_MISMATCH")

    async def test_missing_aggregate_position_symbol_rejected(self):
        r = await crosscheck_symbol_scoped_inventory(
            Probe({}, {}), [{"positionId": "10"}], [])
        self.assertEqual(r["status"], "AGGREGATE_POSITION_SYMBOL_MISSING")

    async def test_missing_aggregate_stop_symbol_rejected(self):
        r = await crosscheck_symbol_scoped_inventory(
            Probe({}, {}), [pos()], [{"positionId": "10", "id": "99"}])
        self.assertEqual(r["status"], "AGGREGATE_STOP_SYMBOL_MISSING")

    async def test_no_observed_symbols_does_not_vacuously_verify(self):
        r = await crosscheck_symbol_scoped_inventory(Probe({}, {}), [], [])
        self.assertFalse(r["symbol_scoped_crosscheck_verified"])
        self.assertEqual(r["status"], "NO_OBSERVED_SYMBOLS")

    async def test_more_than_four_symbols_refused_bounded(self):
        rows = [
            {
                "symbol": f"S{i}_USDT",
                "positionId": str(i),
                "positionType": 2,
                "state": 1,
                "holdVol": 1,
                "holdAvgPrice": 1,
            }
            for i in range(5)
        ]
        probe = Probe({}, {})
        r = await crosscheck_symbol_scoped_inventory(probe, rows, [])
        self.assertEqual(r["status"], "BOUNDED_SYMBOL_CROSSCHECK_REFUSED")
        self.assertEqual(probe.calls, [])

    async def test_two_symbols_are_checked_independently(self):
        p1 = pos()
        p2 = {
            "symbol": "OTHER_USDT",
            "positionId": "11",
            "positionType": 2,
            "state": 1,
            "holdVol": "1",
            "holdAvgPrice": "50",
        }
        probe = Probe(
            {"SIREN_USDT": [p1], "OTHER_USDT": [p2]},
            {"SIREN_USDT": [], "OTHER_USDT": []},
        )
        r = await crosscheck_symbol_scoped_inventory(probe, [p1, p2], [])
        self.assertTrue(r["symbol_scoped_crosscheck_verified"])
        self.assertEqual(r["symbols_checked"], 2)
