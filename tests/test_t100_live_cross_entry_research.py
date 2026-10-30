"""Offline tests: research-only MEXC CROSS order body, never an order POST."""
from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_cross_entry_research import (
    assess_cross_entry_offline, _hypothetical_payload,
)
from app.t100_live_execution import EntryIntent, LiveCaps


def intent():
    return EntryIntent.build(
        signal_id=37, symbol="SIREN_USDT",
        reference_price="100", notional_usdt="500",
        contract_size="1", volume_step="1",
        min_volume="1", max_volume="100",
        price_tick="0.1",
    )


def inventory(*, positions=0, orders=0, reconciled=True, missing_stop=False):
    return {
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "after_positions": positions,
        "reported_open_order_counts": {"sumCount": orders},
        "account_reconciled": reconciled,
        "observed_nonflat_without_current_tpsl": missing_stop,
    }


def mode(cross=True):
    return {
        "status": (
            "MARGIN_MODE_CURRENT_CROSS_CORROBORATED_NONAUTHORIZING" if cross
            else "MARGIN_MODE_CURRENT_ISOLATED_CORROBORATED_NONAUTHORIZING"
        ),
        "independent_mode_reads_agree": True,
        "private_cross_mode_corroborated": cross,
    }


def tier(good=True):
    return {
        "status": (
            "CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING" if good
            else "CANDIDATE_TIER_PRIVATE_CROSS_NOT_OBSERVED"
        ),
        "private_risk_two_get_consistent": good,
        "candidate_quantity_evaluated": good,
        "current_private_mmr_not_below_public": good,
        "private_current_volume_cap_covers_candidate": good,
        "higher_tier_requires_recheck": False,
    }


def review(**overrides):
    args = {
        "contract_size": "1", "volume_step": "1", "price_tick": "0.1",
        "leverage": 1, "inventory": inventory(),
        "margin_mode": mode(), "candidate_tier": tier(),
    }
    args.update(overrides)
    return assess_cross_entry_offline(intent(), **args)


class CrossEntryResearchTests(unittest.TestCase):
    def test_cross_payload_differs_from_existing_isolated_transport(self):
        payload = _hypothetical_payload(
            intent(), contract_size="1", volume_step="1",
            price_tick="0.1", leverage=1,
        )
        self.assertEqual(payload["symbol"], "SIREN_USDT")
        self.assertEqual(payload["side"], 3)
        self.assertEqual(payload["type"], 5)
        self.assertEqual(payload["openType"], 2)
        self.assertEqual(payload["positionMode"], 1)
        self.assertEqual(D(payload["stopLossPrice"]), D("175"))
        self.assertEqual(payload["lossTrend"], 1)
        self.assertEqual(payload["vol"], "5")
        self.assertTrue(payload["externalOid"].startswith("t100v1e"))
        # The actual, currently guarded transport remains isolated at 1x.
        with self.assertRaisesRegex(ValueError, "isolated 1x"):
            LiveCaps(
                max_order_usdt=D("100"), max_portfolio_usdt=D("100"),
                max_daily_loss_usdt=D("1"), margin_type=2,
            )

    def test_all_current_evidence_true_still_never_authorizes(self):
        outcome = review()
        self.assertEqual(outcome.status, "CROSS_ENTRY_RESEARCH_REVIEW_BLOCKED")
        self.assertTrue(outcome.hypothetical_payload_shape_valid)
        self.assertTrue(outcome.current_account_flat_and_order_free)
        self.assertTrue(outcome.private_current_cross_observed)
        self.assertTrue(outcome.candidate_tier_currently_compared)
        self.assertIn("CROSS_MARGIN_ADAPTER_NOT_IMPLEMENTED", outcome.blockers)
        self.assertIn("ACCOUNT_SPECIFIC_API_ORDER_ACCESS_UNVERIFIED",
                      outcome.blockers)
        self.assertIn(
            "ATTACHED_STOP_ATOMICITY_AND_FILL_COVERAGE_UNVERIFIED",
            outcome.blockers,
        )
        for flag in (
            "account_specific_api_order_access_verified",
            "venue_order_acceptance_verified", "stop_attached_atomically_verified",
            "native_stop_protectiveness_verified", "stressed_tiers_verified",
            "full_account_collateral_verified", "cross_margin_adapter_implemented",
            "entry_allowed", "live_authorization", "mexc_orders_sent",
        ):
            self.assertFalse(getattr(outcome, flag))
        self.assertNotIn("SIREN_USDT", str(outcome))
        self.assertNotIn("500", str(outcome))

    def test_actual_nonflat_no_stop_unrelated_order_and_isolated_all_block(self):
        observed = review(
            inventory=inventory(
                positions=1, orders=1, reconciled=False, missing_stop=True,
            ),
            margin_mode=mode(False), candidate_tier=tier(False),
        )
        for code in (
            "EXISTING_POSITION_WITHOUT_OBSERVED_NATIVE_STOP",
            "FIRST_PILOT_REQUIRES_FLAT_ACCOUNT",
            "FIRST_PILOT_REQUIRES_NO_OPEN_ORDERS",
            "ACCOUNT_NOT_RECONCILED",
            "CROSS_MARGIN_PRIVATE_MODE_NOT_CORROBORATED",
            "CANDIDATE_CURRENT_PRIVATE_TIER_NOT_VERIFIED",
        ):
            self.assertIn(code, observed.blockers)
        self.assertFalse(observed.current_account_flat_and_order_free)
        self.assertFalse(observed.live_authorization)

    def test_incomplete_or_stale_inventory_never_claims_flat(self):
        for bad in (
            None, {},
            {"account_wide_completeness_verified": True},
            inventory(positions=0, orders=0, reconciled=False),
            dict(inventory(), critical_inventory_equal_across_witness_window=False),
        ):
            with self.subTest(bad=bad):
                result = review(inventory=bad)
                self.assertFalse(result.current_account_flat_and_order_free)
                self.assertFalse(result.entry_allowed)

    def test_offgrid_quantity_or_stop_invalid(self):
        good = intent()
        for bad in (
            replace(good, stop_price=D("170")),
            replace(good, stop_price=D("175.05")),
            replace(good, volume_contracts=D("5.5")),
            replace(good, volume_contracts=D("6")),
            replace(good, notional_usdt=D("1")),
            replace(good, reference_price=D("NaN")),
            replace(good, symbol="BTC_USDT"),
            replace(good, external_oid="invalid"),
        ):
            with self.subTest(bad=bad):
                out = assess_cross_entry_offline(
                    bad, contract_size="1", volume_step="1",
                    price_tick="0.1", leverage=1,
                    inventory=inventory(), margin_mode=mode(),
                    candidate_tier=tier(),
                )
                self.assertEqual(
                    out.status, "CROSS_RESEARCH_INTENT_INVALID_OR_UNVERIFIED"
                )
                self.assertFalse(out.hypothetical_payload_shape_valid)

    def test_nonfinite_params_and_unapproved_leverage_rejected(self):
        for override in (
            {"contract_size": "NaN"}, {"volume_step": "0"},
            {"price_tick": None}, {"leverage": 0}, {"leverage": 3},
            {"leverage": True},
        ):
            with self.subTest(override=override):
                r = review(**override)
                self.assertFalse(r.hypothetical_payload_shape_valid)
                self.assertFalse(r.live_authorization)

    def test_round_up_stop_to_exact_tick(self):
        i = EntryIntent.build(
            signal_id=1, symbol="SIREN_USDT",
            reference_price="1.01", notional_usdt="101",
            contract_size="1", volume_step="1",
            min_volume="1", max_volume="1000",
            price_tick="0.1",
        )
        payload = _hypothetical_payload(
            i, contract_size="1", volume_step="1",
            price_tick="0.1", leverage=1,
        )
        self.assertEqual(payload["stopLossPrice"], "1.8")

    def test_no_adapter_or_network_imports_reachable(self):
        import inspect
        import app.t100_live_cross_entry_research as source
        import app.t100_live_mexc_adapter as existing_adapter
        import app.t100_live_guards as live_guards
        text = inspect.getsource(source)
        self.assertNotIn(
            "t100_live_cross_entry_research",
            inspect.getsource(existing_adapter),
        )
        self.assertEqual(live_guards.LIVE_ADAPTER_STATUS, "NOT_IMPLEMENTED")
        for banned in ("._private_post(", ".submit_short_with_stop(",
                       "MexcTradeClient(", "httpx.", "asyncpg."):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
