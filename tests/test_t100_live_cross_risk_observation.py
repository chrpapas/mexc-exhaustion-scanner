"""Offline two-read cross-risk co-observation tests. No MEXC credentials or POST."""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D
from dataclasses import replace

from app.t100_live_cross_risk_observation import (
    CrossRiskRead, observe_cross_risk_window,
)
from tests.test_t100_live_risk_tier_witness import private, public


FIRST_TS = 1_000_000
SECOND_TS = 1_000_450


def frame(ts=FIRST_TS):
    risk = private()
    contract = public()
    return CrossRiskRead(
        observed_at_ms=ts,
        positions=[{
            "symbol": "TEST_USDT", "positionId": "12",
            "positionType": 2, "openType": 2,
            "holdVol": "50", "holdAvgPrice": "2",
            "im": "20", "unRealizedPnl": "0",
            "leverage": 5,
        }],
        stops=[{
            "id": "90", "positionId": "12", "symbol": "TEST_USDT",
            "stopLossPrice": "3.5", "state": 1,
        }],
        assets=[{
            "currency": "USDT", "cashBalance": "2000",
            "equity": "2000", "availableBalance": "1980",
            "frozenBalance": "0", "positionMargin": "20",
            "unrealized": "0", "bonus": "0",
            "debtAmount": "0", "contributeMarginAmount": "0",
        }],
        private_risk_by_symbol=risk,
        public_contract_by_symbol={"TEST_USDT": contract},
        fair_by_symbol={"TEST_USDT": {
            "fairPrice": "2.0", "observed_at_ms": ts,
        }},
        positions_complete=True,
        stops_complete=True,
        assets_complete=True,
    )


class Reader:
    def __init__(self, first=None, second=None):
        self.frames = [
            copy.deepcopy(first if first is not None else frame()),
            copy.deepcopy(second if second is not None else frame(SECOND_TS)),
        ]
        self.calls = 0

    async def read(self):
        self.calls += 1
        return copy.deepcopy(self.frames.pop(0))


def observe(reader, *, now=1_001_000):
    return observe_cross_risk_window(reader, now_ms=lambda: now)


class CrossRiskObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_good_window_is_evidence_only_not_a_liquidation_certificate(self):
        reader = Reader()
        result = await observe(reader)
        self.assertEqual(result.status, "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING")
        self.assertEqual(result.observed_positions, 1)
        self.assertEqual(reader.calls, 2)
        self.assertTrue(result.stable_critical_inventory)
        self.assertTrue(result.position_margin_economic_witness_observed)
        self.assertTrue(result.current_risk_tiers_observed)
        self.assertTrue(result.fair_prices_observed)
        self.assertEqual(len(result.evidence_fingerprint_sha256), 64)
        self.assertFalse(result.account_wide_completeness_certified)
        self.assertFalse(result.future_stressed_tiers_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)

    async def test_small_fair_move_is_not_critical_state_drift(self):
        second = frame(SECOND_TS)
        second.fair_by_symbol["TEST_USDT"]["fairPrice"] = "2.01"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING")

    async def test_large_mark_move_fails_closed(self):
        second = frame(SECOND_TS)
        second.fair_by_symbol["TEST_USDT"]["fairPrice"] = "2.2"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_FAIR_PRICE_DRIFT")

    async def test_private_tier_changed_in_witness_window(self):
        second = frame(SECOND_TS)
        second.private_risk_by_symbol["TEST_USDT"][0]["mmr"] = 0.02
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_CRITICAL_STATE_DRIFT")

    async def test_contract_metadata_changed_in_witness_window(self):
        second = frame(SECOND_TS)
        second.public_contract_by_symbol["TEST_USDT"]["liquidationFeeRate"] = 0.006
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_CRITICAL_STATE_DRIFT")

    async def test_account_cash_changed_during_witness_window(self):
        second = frame(SECOND_TS)
        second.assets[0]["cashBalance"] = "1900"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_CRITICAL_STATE_DRIFT")

    async def test_quantity_changed_during_witness_window(self):
        second = frame(SECOND_TS)
        second.positions[0]["holdVol"] = "51"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_CRITICAL_STATE_DRIFT")

    async def test_unreconciled_pnl_blocks_even_when_raw_rows_are_well_formed(self):
        second = frame(SECOND_TS)
        second.assets[0]["unrealized"] = "1"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_unowned_position_not_promoted_to_ownership(self):
        r = await observe(Reader())
        self.assertFalse(r.account_wide_completeness_certified)
        self.assertFalse(r.entry_allowed)

    async def test_missing_native_stop_blocks(self):
        second = frame(SECOND_TS)
        second.stops.clear()
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_wrong_stop_position_id_blocks(self):
        second = frame(SECOND_TS)
        second.stops[0]["positionId"] = "999"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_failure_stage_identifies_second_frame_without_raw_details(self):
        second = frame(SECOND_TS)
        second.positions[0]["openType"] = 1
        result = await observe(Reader(second=second))
        self.assertEqual(result.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")
        self.assertEqual(result.failure_stage, "VALIDATE_SECOND")
        self.assertEqual(result.failure_code, "POSITION_NOT_VERIFIED_CROSS_SHORT")
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)

    async def test_zero_available_or_withdrawable_balance_is_valid_observation(self):
        first = frame()
        second = frame(SECOND_TS)
        for x in (first, second):
            x.assets[0]["cashBalance"] = "0"
            x.assets[0]["availableBalance"] = "0"
        result = await observe(Reader(first=first, second=second))
        self.assertEqual(result.status, "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING")
        self.assertFalse(result.venue_liquidation_certified)

    async def test_negative_cash_balance_blocks_with_fixed_code(self):
        second = frame(SECOND_TS)
        second.assets[0]["cashBalance"] = "-1"
        r = await observe(Reader(second=second))
        self.assertEqual(r.failure_stage, "VALIDATE_SECOND")
        self.assertEqual(r.failure_code, "BAD_DECIMAL")

    async def test_unsupported_current_tier_schedule_reports_specific_safe_code(self):
        second = frame(SECOND_TS)
        second.public_contract_by_symbol["TEST_USDT"]["riskLimitMode"] = "INCREASE"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")
        self.assertEqual(r.failure_stage, "VALIDATE_SECOND")
        self.assertEqual(
            r.failure_code, "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE"
        )
        self.assertFalse(r.live_authorization)

    async def test_isolated_position_blocks(self):
        second = frame(SECOND_TS)
        second.positions[0]["openType"] = 1
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_risk_missing_for_exact_position_symbol_blocks(self):
        second = frame(SECOND_TS)
        second = replace(second, private_risk_by_symbol={})
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_missing_or_old_fair_price_blocks(self):
        second = frame(SECOND_TS)
        second.fair_by_symbol["TEST_USDT"] = {"fairPrice": "2", "observed_at_ms": -1000}
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_invalid_fair_price_blocks(self):
        second = frame(SECOND_TS)
        second.fair_by_symbol["TEST_USDT"]["fairPrice"] = "NaN"
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_missing_complete_collection_flags_blocks(self):
        second = frame(SECOND_TS)
        second = replace(second, positions_complete=False)
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_nonconsecutive_or_stale_frame_blocks(self):
        second = frame(FIRST_TS)
        r = await observe(Reader(second=second))
        self.assertEqual(r.status, "CROSS_RISK_READ_WINDOW_NOT_CONSECUTIVE")
        r = await observe(Reader(), now=1_010_000)
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")

    async def test_reader_timeout_fails_closed(self):
        class Broken:
            async def read(self):
                raise TimeoutError("private details not to be logged")
        r = await observe(Broken())
        self.assertEqual(r.status, "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")
        self.assertNotIn("private details", str(r))

    async def test_invalid_window_config_blocks_before_reads(self):
        rd = Reader()
        r = await observe_cross_risk_window(
            rd, now_ms=lambda: 1_001_000,
            max_separation_ms=0,
        )
        self.assertEqual(r.status, "CROSS_RISK_WINDOW_CONFIG_INVALID")
        self.assertEqual(rd.calls, 0)


if __name__ == "__main__":
    unittest.main()
