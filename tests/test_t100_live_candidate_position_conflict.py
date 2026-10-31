"""Pure offline safety checks for candidate flat-vs-existing-position screen."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_candidate_position_conflict import (
    inspect_candidate_position_conflicts,
)
from app.t100_live_candidate_position_conflict_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


def position(*, mode=1, symbol="TEST_USDT", side=2, vol=1):
    return {
        "symbol": symbol, "positionId": 1234, "positionType": side,
        "openType": mode, "holdVol": vol, "frozenVol": 0,
        "state": 1, "leverage": 5,
    }


def counts(*, stop=0):
    return {
        "sumCount": stop, "limitOrderCount": 0,
        "stopOrderCount": stop, "planOrderCount": 0,
        "trackOrderCount": 0,
    }


class FakePrivate:
    def __init__(self, positions1=None, positions2=None,
                 counts1=None, counts2=None, fail_counts=False):
        self.positions = [copy.deepcopy(positions1 if positions1 is not None else []),
                          copy.deepcopy(positions2 if positions2 is not None
                                        else positions1 if positions1 is not None else [])]
        self.counts = [copy.deepcopy(counts1 if counts1 is not None else counts()),
                       copy.deepcopy(counts2 if counts2 is not None
                                     else counts1 if counts1 is not None else counts())]
        self.calls = []
        self.pidx = 0
        self.cidx = 0
        self.fail_counts = fail_counts

    async def _private_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/api/v1/private/position/open_positions":
            value = copy.deepcopy(self.positions[self.pidx])
            self.pidx += 1
            return value
        if path == "/api/v1/private/order/open_order_total_count":
            if self.fail_counts:
                raise RuntimeError("SECRET_ORDER_ID")
            value = copy.deepcopy(self.counts[self.cidx])
            self.cidx += 1
            return value
        raise AssertionError("Unexpected endpoint")


class PositionConflictTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, fake):
        return await inspect_candidate_position_conflicts(
            ReadOnlyMexcProbe(fake), "TEST_USDT",
        )

    async def test_no_candidate_position_zero_orders_nonauthorizing(self):
        private = FakePrivate()
        report = await self.inspect(private)
        self.assertEqual(
            report.status, "CANDIDATE_NO_POSITION_ACCOUNT_ORDER_FREE_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(report.candidate_no_open_positions_observed)
        self.assertTrue(report.symbol_scoped_positions_two_get_consistent)
        self.assertTrue(report.account_order_counts_two_get_consistent)
        self.assertEqual(report.account_open_orders_observed, 0)
        self.assertFalse(report.account_wide_completeness_certified)
        self.assertFalse(report.candidate_symbol_open_orders_certified_absent)
        self.assertFalse(report.private_cross_margin_ready)
        self.assertFalse(report.margin_mode_change_required_or_allowed)
        self.assertTrue(report.existing_positions_untouched)
        self.assertFalse(report.live_authorization)
        self.assertFalse(report.mexc_orders_sent)
        self.assertEqual([x[0] for x in private.calls], [
            "/api/v1/private/position/open_positions",
            "/api/v1/private/order/open_order_total_count",
            "/api/v1/private/position/open_positions",
            "/api/v1/private/order/open_order_total_count",
        ])
        self.assertEqual(private.calls[0][1], {"symbol": "TEST_USDT"})
        self.assertEqual(private.calls[1][1], {})

    async def test_no_position_manual_stop_orders_accountwide_unscoped(self):
        report = await self.inspect(FakePrivate(counts1=counts(stop=1)))
        self.assertEqual(report.status,
                         "CANDIDATE_NO_POSITION_ACCOUNT_ORDERS_PRESENT_UNSCOPED")
        self.assertTrue(report.candidate_no_open_positions_observed)
        self.assertEqual(report.account_open_orders_observed, 1)
        self.assertFalse(report.candidate_symbol_open_orders_certified_absent)
        self.assertFalse(report.entry_allowed)

    async def test_existing_isolated_short_blocks_any_cross_claim(self):
        report = await self.inspect(FakePrivate(positions1=[position()]))
        self.assertEqual(report.status,
                         "CANDIDATE_EXISTING_POSITION_PRESENT_NONAUTHORIZING")
        self.assertEqual(report.candidate_short_mode, "ISOLATED")
        self.assertTrue(report.candidate_short_position_observed)
        self.assertFalse(report.private_cross_margin_ready)
        self.assertFalse(report.margin_mode_change_required_or_allowed)

    async def test_existing_cross_short_also_non_authorizing(self):
        report = await self.inspect(FakePrivate(positions1=[position(mode=2)]))
        self.assertEqual(report.candidate_short_mode, "CROSS")
        self.assertFalse(report.entry_allowed)

    async def test_position_state_drift_rejected(self):
        report = await self.inspect(
            FakePrivate(positions1=[], positions2=[position()])
        )
        self.assertEqual(report.status, "CANDIDATE_POSITION_CRITICAL_DRIFT")
        self.assertFalse(report.candidate_no_open_positions_observed)

    async def test_account_count_drift_rejected(self):
        report = await self.inspect(
            FakePrivate(counts1=counts(), counts2=counts(stop=1))
        )
        self.assertEqual(report.status, "ACCOUNT_ORDER_COUNT_DRIFT")
        self.assertFalse(report.account_order_counts_two_get_consistent)

    async def test_wrong_symbol_not_interpreted_as_candidate_position(self):
        report = await self.inspect(
            FakePrivate(positions1=[position(symbol="OTHER_USDT")])
        )
        self.assertEqual(report.status,
                         "CANDIDATE_POSITION_GET_OR_SCHEMA_FAILED")
        self.assertFalse(report.entry_allowed)
        self.assertNotIn("OTHER_USDT", str(report))

    async def test_bad_private_count_is_fail_closed_and_redacted(self):
        report = await self.inspect(FakePrivate(fail_counts=True))
        self.assertEqual(report.status,
                         "ACCOUNT_ORDER_COUNTS_GET_OR_SCHEMA_FAILED")
        self.assertNotIn("SECRET", str(report))

    async def test_symbol_validation_precedes_network(self):
        fake = FakePrivate()
        report = await inspect_candidate_position_conflicts(
            ReadOnlyMexcProbe(fake), "../SECRET",
        )
        self.assertEqual(report.status, "CANDIDATE_POSITION_SYMBOL_INVALID")
        self.assertEqual(fake.calls, [])

    async def test_cli_paper_kill_and_missing_credentials(self):
        for env in ({}, {"T100_EXECUTION_MODE": "live",
                         "T100_LIVE_ORDER_API_ENABLED": "false",
                         "T100_LIVE_KILL_SWITCH": "true"}):
            result = await run("TEST_USDT", environ=env)
            self.assertEqual(result["status"], "CANDIDATE_POSITION_PAPER_KILL_REQUIRED")
            self.assertFalse(result["mexc_orders_sent"])
        report = await run("TEST_USDT", environ={
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        })
        self.assertEqual(report["status"], "CANDIDATE_POSITION_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()
