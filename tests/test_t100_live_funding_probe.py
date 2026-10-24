"""Offline MEXC funding pagination and redaction tests."""
import unittest

from app.t100_live_funding_probe import (
    collect_observed_position_funding, assess_funding_economics,
)
from app.t100_live_pagination import PaginationHalted


class FakeProbe:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def raw_funding_page(self, position_id, *, page, size):
        self.calls.append((position_id, page, size))
        return self.pages[page]


class FundingProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_page_complete(self):
        probe = FakeProbe({
            1: {
                "pageSize": 100,
                "totalCount": 2,
                "totalPage": 1,
                "currentPage": 1,
                "resultList": [
                    {"id": 1, "symbol": "SECRET_USDT", "funding": -1.2,
                     "rate": 0.001, "positionType": 2,
                     "positionValue": 100, "settleTime": 1000},
                    {"id": 2, "symbol": "SECRET_USDT", "funding": 0.5,
                     "rate": -0.001, "positionType": 2,
                     "positionValue": 90, "settleTime": 2000},
                ],
            },
        })
        r = await collect_observed_position_funding(
            probe, position_id="10")
        self.assertTrue(r["pagination_verified_for_observed_position"])
        self.assertEqual(r["reported_total_rows"], 2)
        self.assertEqual(r["page_count"], 1)
        self.assertEqual(r["schema"]["rows_seen"], 2)
        self.assertFalse(r["funding_sign_semantics_verified"])
        self.assertFalse(r["account_wide_funding_completeness_verified"])
        self.assertNotIn("SECRET_USDT", str(r))
        self.assertNotIn("-1.2", str(r))

    async def test_multiple_pages_complete(self):
        rows1 = [{"id": i} for i in range(1, 101)]
        rows2 = [{"id": 101}]
        probe = FakeProbe({
            1: {
                "pageSize": 100, "totalCount": 101, "totalPage": 2,
                "currentPage": 1, "resultList": rows1,
            },
            2: {
                "pageSize": 100, "totalCount": 101, "totalPage": 2,
                "currentPage": 2, "resultList": rows2,
            },
        })
        r = await collect_observed_position_funding(
            probe, position_id="10")
        self.assertEqual(r["reported_total_rows"], 101)
        self.assertEqual(r["page_count"], 2)
        self.assertEqual(len(probe.calls), 2)

    async def test_empty_page_can_report_zero_or_one_total_page(self):
        for total_page in (0, 1):
            probe = FakeProbe({
                1: {
                    "pageSize": 100, "totalCount": 0,
                    "totalPage": total_page, "currentPage": 1,
                    "resultList": [],
                },
            })
            r = await collect_observed_position_funding(
                probe, position_id="10")
            self.assertTrue(r["pagination_verified_for_observed_position"])
            self.assertEqual(r["reported_total_rows"], 0)

    async def test_total_page_mismatch_blocks(self):
        probe = FakeProbe({
            1: {
                "pageSize": 100, "totalCount": 101, "totalPage": 1,
                "currentPage": 1,
                "resultList": [{"id": i} for i in range(1, 101)],
            },
        })
        with self.assertRaisesRegex(PaginationHalted,
                                    "FUNDING_TOTAL_PAGE_MISMATCH"):
            await collect_observed_position_funding(
                probe, position_id="10")

    async def test_current_page_mismatch_blocks(self):
        probe = FakeProbe({
            1: {
                "pageSize": 100, "totalCount": 1, "totalPage": 1,
                "currentPage": 2, "resultList": [{"id": 1}],
            },
        })
        with self.assertRaisesRegex(PaginationHalted,
                                    "FUNDING_CURRENT_PAGE_MISMATCH"):
            await collect_observed_position_funding(
                probe, position_id="10")

    async def test_duplicate_ids_block(self):
        probe = FakeProbe({
            1: {
                "pageSize": 100, "totalCount": 2, "totalPage": 1,
                "currentPage": 1,
                "resultList": [{"id": 1}, {"id": 1}],
            },
        })
        with self.assertRaisesRegex(PaginationHalted, "DUPLICATE_ROW"):
            await collect_observed_position_funding(
                probe, position_id="10")

    async def test_missing_id_blocks(self):
        probe = FakeProbe({
            1: {
                "pageSize": 100, "totalCount": 1, "totalPage": 1,
                "currentPage": 1, "resultList": [{}],
            },
        })
        with self.assertRaisesRegex(PaginationHalted, "DUPLICATE_ROW|IDENTITY"):
            await collect_observed_position_funding(
                probe, position_id="10")


class FundingEconomicsTests(unittest.TestCase):
    def test_long_positive_rate_is_debit(self):
        r = assess_funding_economics([{
            "positionType": 1, "positionValue": 100,
            "rate": 0.001, "funding": -0.1,
        }])
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertTrue(r["funding_formula_semantics_observed"])

    def test_long_negative_rate_is_credit(self):
        r = assess_funding_economics([{
            "positionType": 1, "positionValue": 100,
            "rate": -0.001, "funding": 0.1,
        }])
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertTrue(r["funding_formula_semantics_observed"])

    def test_short_positive_rate_is_credit(self):
        r = assess_funding_economics([{
            "positionType": 2, "positionValue": 100,
            "rate": 0.001, "funding": 0.1,
        }])
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertTrue(r["funding_formula_semantics_observed"])

    def test_short_negative_rate_is_debit(self):
        r = assess_funding_economics([{
            "positionType": 2, "positionValue": 100,
            "rate": -0.001, "funding": -0.1,
        }])
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertTrue(r["funding_formula_semantics_observed"])

    def test_wrong_cashflow_sign_is_detected(self):
        r = assess_funding_economics([{
            "positionType": 2, "positionValue": 100,
            "rate": 0.001, "funding": -0.1,
        }])
        self.assertFalse(r["funding_cashflow_sign_semantics_observed"])
        self.assertEqual(r["status"], "FUNDING_SIGN_SEMANTIC_MISMATCH")

    def test_formula_drift_is_separate_from_sign(self):
        r = assess_funding_economics([{
            "positionType": 2, "positionValue": 100,
            "rate": 0.001, "funding": 0.2,
        }])
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertFalse(r["funding_formula_semantics_observed"])
        self.assertEqual(r["status"],
                         "FUNDING_SIGN_CONSISTENT_FORMULA_DRIFT")

    def test_all_rows_must_be_consistent(self):
        r = assess_funding_economics([
            {"positionType": 1, "positionValue": 100,
             "rate": -0.001, "funding": 0.1},
            {"positionType": 2, "positionValue": 100,
             "rate": 0.001, "funding": 0.1},
        ])
        self.assertEqual(r["nonzero_rows_examined"], 2)
        self.assertEqual(r["sign_consistent_rows"], 2)
        self.assertEqual(r["magnitude_consistent_rows"], 2)
        self.assertTrue(r["funding_cashflow_sign_semantics_observed"])
        self.assertTrue(r["funding_formula_semantics_observed"])

    def test_zero_rate_zero_funding_is_neutral(self):
        r = assess_funding_economics([{
            "positionType": 2, "positionValue": 100,
            "rate": 0, "funding": 0,
        }])
        self.assertEqual(r["zero_rate_or_funding_rows"], 1)
        self.assertFalse(r["funding_cashflow_sign_semantics_observed"])

    def test_invalid_side_fails_closed(self):
        r = assess_funding_economics([{
            "positionType": 9, "positionValue": 100,
            "rate": 0.001, "funding": 0.1,
        }])
        self.assertEqual(r["status"], "FUNDING_ROW_SEMANTICS_INVALID")
