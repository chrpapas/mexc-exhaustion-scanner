"""Offline/no-network candidate current TP/SL order-scope tests."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_candidate_stop_scope import inspect_candidate_stop_scope
from app.t100_live_candidate_stop_scope_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


def stop(symbol="OTHER_USDT", number=11, state=1):
    return {
        "id": number, "symbol": symbol, "positionId": 77, "orderId": 0,
        "positionType": 2, "state": state, "isFinished": 0,
        "vol": 1, "stopLossPrice": "0.09", "takeProfitPrice": 0,
        "version": 1, "updateTime": 1000,
    }


def counts(stop_orders=1, limit=0, plan=0, track=0):
    return {
        "sumCount": stop_orders + limit + plan + track,
        "stopOrderCount": stop_orders, "limitOrderCount": limit,
        "planOrderCount": plan, "trackOrderCount": track,
    }


class Private:
    def __init__(self, *, positions=None, global_stops=None,
                 scoped_stops=None, order_counts=None):
        self.positions = positions if positions is not None else [[], []]
        self.global_stops = global_stops if global_stops is not None else [
            [stop()], [stop()],
        ]
        self.scoped_stops = scoped_stops if scoped_stops is not None else [
            [], [],
        ]
        self.order_counts = order_counts if order_counts is not None else [
            counts(), counts(),
        ]
        self.index = {"positions": 0, "global": 0, "scoped": 0, "counts": 0}
        self.calls = []

    async def _private_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/api/v1/private/position/open_positions":
            target = "positions"
            data = self.positions
        elif path == "/api/v1/private/order/open_order_total_count":
            target = "counts"
            data = self.order_counts
        elif path == "/api/v1/private/stoporder/open_orders":
            target = "scoped" if params else "global"
            data = self.scoped_stops if params else self.global_stops
        else:
            raise RuntimeError("NEVER_EXPOSE_AUTH")
        idx = self.index[target]
        self.index[target] += 1
        return copy.deepcopy(data[idx])


class CandidateStopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self, client):
        return await inspect_candidate_stop_scope(
            ReadOnlyMexcProbe(client), "SIREN_USDT",
        )

    async def test_unrelated_one_manual_tpsl_does_not_implicate_siren(self):
        client = Private()
        r = await self.observe(client)
        self.assertEqual(r.status,
            "CANDIDATE_STOP_SCOPE_NO_POSITION_NO_TPSL_OBSERVED_NONAUTHORIZING")
        self.assertEqual(r.candidate_open_positions, 0)
        self.assertEqual(r.total_open_orders, 1)
        self.assertEqual(r.stop_orders, 1)
        self.assertEqual(r.global_current_stops_observed, 1)
        self.assertEqual(r.candidate_current_stops_observed, 0)
        self.assertTrue(r.global_stop_count_agrees)
        self.assertTrue(r.account_orders_all_tpsl_in_observed_window)
        self.assertTrue(r.candidate_current_tpsl_absent_observed)
        self.assertFalse(r.candidate_all_order_types_absent_certified)
        self.assertFalse(r.account_wide_completeness_certified)
        self.assertFalse(r.native_stop_protection_verified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)
        self.assertEqual(len(client.calls), 8)
        self.assertTrue(all(path.startswith("/api/v1/private/")
                            for path, _ in client.calls))
        self.assertTrue(all(
            params == {"symbol": "SIREN_USDT"}
            for path, params in client.calls
            if path == "/api/v1/private/position/open_positions"
            or (path == "/api/v1/private/stoporder/open_orders" and params)
        ))
        self.assertNotIn("OTHER_USDT", str(r))
        self.assertNotIn("SIREN_USDT", str(r))

    async def test_candidate_open_stop_not_discarded(self):
        target = stop("SIREN_USDT", 19)
        client = Private(
            global_stops=[[target], [target]],
            scoped_stops=[[target], [target]],
        )
        r = await self.observe(client)
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_CANDIDATE_TPSL_PRESENT")
        self.assertEqual(r.candidate_current_stops_observed, 1)
        self.assertFalse(r.candidate_current_tpsl_absent_observed)
        self.assertFalse(r.entry_allowed)

    async def test_mismatched_account_count_fails_closed(self):
        client = Private(order_counts=[counts(stop_orders=2),
                                      counts(stop_orders=2)])
        r = await self.observe(client)
        self.assertEqual(r.status,
            "CANDIDATE_STOP_SCOPE_ACCOUNT_STOP_COUNT_MISMATCH")
        self.assertFalse(r.global_stop_count_agrees)

    async def test_other_order_type_requires_separate_scope_read(self):
        client = Private(
            global_stops=[[], []], scoped_stops=[[], []],
            order_counts=[counts(stop_orders=0, limit=1),
                          counts(stop_orders=0, limit=1)],
        )
        r = await self.observe(client)
        self.assertEqual(r.status,
            "CANDIDATE_STOP_SCOPE_OTHER_ORDER_TYPES_UNSCOPED")
        self.assertFalse(r.account_orders_all_tpsl_in_observed_window)

    async def test_current_position_blocks_conflict_clearance(self):
        p = {
            "positionId": 15, "symbol": "SIREN_USDT", "positionType": 2,
            "openType": 1, "state": 1, "holdVol": 3,
        }
        client = Private(positions=[[p], [p]])
        r = await self.observe(client)
        self.assertEqual(r.status,
                         "CANDIDATE_STOP_SCOPE_EXISTING_POSITION_PRESENT")
        self.assertEqual(r.candidate_open_positions, 1)
        self.assertFalse(r.entry_allowed)

    async def test_stop_drift_detected_even_if_count_is_same(self):
        s1, s2 = stop(), stop(number=12)
        r = await self.observe(Private(global_stops=[[s1], [s2]]))
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_GLOBAL_STOP_DRIFT")

    async def test_order_count_drift_fails_closed(self):
        r = await self.observe(Private(
            order_counts=[counts(), counts(limit=1)],
        ))
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_COUNTS_DRIFT")

    async def test_global_and_scoped_disagree(self):
        r = await self.observe(Private(
            global_stops=[[stop()], [stop()]],
            scoped_stops=[[stop("SIREN_USDT", 55)],
                          [stop("SIREN_USDT", 55)]],
        ))
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_GLOBAL_SCOPED_MISMATCH")

    async def test_nonactive_stop_row_fails(self):
        r = await self.observe(Private(
            global_stops=[[stop(state=2)], [stop(state=2)]],
        ))
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_NONACTIVE_ROWS")

    async def test_duplicate_stop_id_fails(self):
        both = [stop(number=11), stop("OTHER_USDT", 11)]
        r = await self.observe(Private(
            global_stops=[both, both], order_counts=[counts(2), counts(2)]
        ))
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_READ_OR_SCHEMA_FAILED")

    async def test_invalid_symbol_no_network(self):
        client = Private()
        r = await inspect_candidate_stop_scope(
            ReadOnlyMexcProbe(client), "../BAD",
        )
        self.assertEqual(r.status, "CANDIDATE_STOP_SCOPE_SYMBOL_INVALID")
        self.assertEqual(client.calls, [])

    async def test_cli_paper_kill_and_missing_creds(self):
        for env in ({}, {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }):
            r = await run("SIREN_USDT", environ=env)
            self.assertEqual(r["status"], "CANDIDATE_STOP_SCOPE_PAPER_KILL_REQUIRED")
        env = {
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }
        r = await run("SIREN_USDT", environ=env)
        self.assertEqual(r["status"], "CANDIDATE_STOP_SCOPE_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()
