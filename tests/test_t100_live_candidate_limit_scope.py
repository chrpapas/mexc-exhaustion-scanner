"""Offline/no-network tests for current MEXC regular limit-order scope."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_candidate_limit_scope import inspect_candidate_limit_orders
from app.t100_live_candidate_limit_scope_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


def order(symbol="OTHER_USDT", oid="101", **updates):
    row = {
        "orderId": oid, "symbol": symbol,
        "side": 3, "category": 1, "orderType": 1,
        "openType": 1, "state": 2, "price": 1.1,
        "vol": 1, "version": 1, "updateTime": 1000,
    }
    row.update(updates)
    return row


def counts(limit=1, stop=0):
    return dict(
        sumCount=limit + stop, limitOrderCount=limit,
        stopOrderCount=stop, planOrderCount=0, trackOrderCount=0,
    )


class Private:
    def __init__(self, positions=None, order_pages=None, order_counts=None):
        self.positions = positions if positions is not None else [[], []]
        self.pages = order_pages if order_pages is not None else [
            [order()], [order()],
        ]
        self.counts = order_counts if order_counts is not None else [
            counts(), counts(), counts(), counts(),
        ]
        self.calls = []
        self.pidx = self.oidx = self.cidx = 0

    async def _private_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/api/v1/private/position/open_positions":
            response = self.positions[self.pidx]
            self.pidx += 1
        elif path == "/api/v1/private/order/open_order_total_count":
            response = self.counts[self.cidx]
            self.cidx += 1
        elif path == "/api/v1/private/order/list/open_orders":
            response = self.pages[self.oidx]
            self.oidx += 1
        else:
            raise RuntimeError("FORBIDDEN_ORDER_MUTATION")
        return copy.deepcopy(response)


class CandidateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, client):
        return await inspect_candidate_limit_orders(
            ReadOnlyMexcProbe(client), "SIREN_USDT",
        )

    async def test_one_other_contract_limit_order_is_not_siren(self):
        client = Private()
        result = await self.inspect(client)
        self.assertEqual(
            result.status,
            "CANDIDATE_LIMIT_ONLY_OTHER_CONTRACT_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result.candidate_positions_observed, 0)
        self.assertEqual(result.account_limit_orders_reported, 1)
        self.assertEqual(result.account_limit_orders_listed, 1)
        self.assertEqual(result.candidate_limit_orders_observed, 0)
        self.assertEqual(result.other_contract_limit_orders_observed, 1)
        self.assertTrue(result.candidate_regular_limit_absent_observed)
        self.assertTrue(result.account_limit_order_count_matches_list)
        self.assertFalse(result.candidate_all_order_types_absent_certified)
        self.assertFalse(result.account_wide_completeness_certified)
        self.assertFalse(result.collateral_certified)
        self.assertFalse(result.private_cross_margin_ready)
        self.assertFalse(result.native_stop_protection_verified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(len(client.calls), 8)
        self.assertEqual([p for p, _ in client.calls], [
            "/api/v1/private/position/open_positions",
            "/api/v1/private/order/open_order_total_count",
            "/api/v1/private/order/list/open_orders",
            "/api/v1/private/order/open_order_total_count",
            "/api/v1/private/position/open_positions",
            "/api/v1/private/order/open_order_total_count",
            "/api/v1/private/order/list/open_orders",
            "/api/v1/private/order/open_order_total_count",
        ])
        self.assertTrue(all(params == {"page_num": 1, "page_size": 100}
                            for path, params in client.calls
                            if path.endswith("/list/open_orders")))
        self.assertNotIn("OTHER_USDT", str(result))
        self.assertNotIn("101", str(result))

    async def test_candidate_order_identified_but_not_changed(self):
        client = Private(order_pages=[
            [order("SIREN_USDT")], [order("SIREN_USDT")],
        ])
        result = await self.inspect(client)
        self.assertEqual(
            result.status,
            "CANDIDATE_LIMIT_CANDIDATE_ORDER_PRESENT_NONAUTHORIZING",
        )
        self.assertEqual(result.candidate_limit_orders_observed, 1)
        self.assertFalse(result.candidate_regular_limit_absent_observed)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.mexc_orders_sent)

    async def test_current_position_not_assumed_flat(self):
        p = {"symbol": "SIREN_USDT", "positionId": 12, "state": 1,
             "positionType": 2, "openType": 1, "holdVol": 5}
        result = await self.inspect(Private(positions=[[p], [p]]))
        self.assertEqual(result.status,
                         "CANDIDATE_LIMIT_POSITION_PRESENT_NONAUTHORIZING")
        self.assertEqual(result.candidate_positions_observed, 1)

    async def test_changed_order_identity_causes_drift(self):
        result = await self.inspect(Private(order_pages=[
            [order(oid="101")], [order(oid="102")],
        ]))
        self.assertEqual(result.status, "CANDIDATE_LIMIT_ORDER_LIST_DRIFT")
        self.assertFalse(result.account_limit_order_count_matches_list)

    async def test_duplicated_or_nonactive_open_order_fails_closed(self):
        for pages in (
            [[order(), order()], [order(), order()]],
            [[order(state=3)], [order(state=3)]],
            [[order(category=2)], [order(category=2)]],
            [[{"symbol": "SIREN_USDT"}], [{"symbol": "SIREN_USDT"}]],
        ):
            with self.subTest(pages=pages):
                result = await self.inspect(Private(order_pages=pages))
                self.assertIn(result.status, {
                    "CANDIDATE_LIMIT_SCHEMA_UNVERIFIED",
                    "CANDIDATE_LIMIT_PAGE_COUNT_MISMATCH",
                })
                self.assertFalse(result.entry_allowed)

    async def test_count_change_inside_window_fails(self):
        result = await self.inspect(Private(order_counts=[
            counts(1), counts(2), counts(2), counts(2),
        ]))
        self.assertEqual(result.status, "CANDIDATE_LIMIT_COUNT_DRIFT")

    async def test_count_change_between_windows_fails(self):
        result = await self.inspect(Private(order_pages=[
            [order()], [order("OTHER_USDT", "101"), order("OTHER_USDT", "102")],
        ], order_counts=[
            counts(1), counts(1), counts(2), counts(2),
        ]))
        self.assertEqual(result.status, "CANDIDATE_LIMIT_ACCOUNT_ORDER_COUNT_DRIFT")

    async def test_one_page_not_accepted_if_reported_count_exceeds_100(self):
        client = Private(order_counts=[counts(101)] * 4)
        result = await self.inspect(client)
        self.assertEqual(result.status, "CANDIDATE_LIMIT_PAGINATION_NOT_BOUNDED")
        self.assertFalse(result.account_limit_order_count_matches_list)
        self.assertEqual(client.oidx, 0)

    async def test_other_order_types_do_not_certify_absence(self):
        result = await self.inspect(Private(order_counts=[counts(1, 1)] * 4))
        self.assertEqual(result.status, "CANDIDATE_LIMIT_OTHER_ORDER_TYPES_UNSCOPED")
        self.assertFalse(result.candidate_all_order_types_absent_certified)

    async def test_invalid_symbol_rejected_before_get(self):
        client = Private()
        result = await inspect_candidate_limit_orders(
            ReadOnlyMexcProbe(client), "../SECRET",
        )
        self.assertEqual(result.status, "CANDIDATE_LIMIT_SYMBOL_INVALID")
        self.assertEqual(client.calls, [])

    async def test_cli_requires_paper_kill_and_credentials(self):
        for env in ({}, {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }):
            report = await run("SIREN_USDT", environ=env)
            self.assertEqual(report["status"], "CANDIDATE_LIMIT_PAPER_KILL_REQUIRED")
        report = await run("SIREN_USDT", environ={
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        })
        self.assertEqual(report["status"], "CANDIDATE_LIMIT_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()
