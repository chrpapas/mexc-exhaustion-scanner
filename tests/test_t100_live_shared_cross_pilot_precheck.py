"""Offline shared-account cross pilot policy: manual positions are NOT ignored."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import unittest

from app.t100_live_execution import EntryIntent
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_shared_cross_pilot_precheck import (
    assess_shared_cross_pilot_precheck,
)
from tests.test_t100_live_four_slot_mexc_readonly import (
    MockMexcGetOnly, prepare_rows,
)


def next_intent(notional="50", symbol="FOURTH_USDT"):
    return EntryIntent.build(
        signal_id=8801, symbol=symbol, reference_price="2",
        notional_usdt=notional, contract_size="1",
        volume_step="1", min_volume="1", max_volume="10000",
        price_tick="0.001",
    )


class SharedCrossPilotTests(unittest.IsolatedAsyncioTestCase):
    async def setup_account(self, n=2):
        _, orders, positions, stops = prepare_rows(n)
        client = MockMexcGetOnly(orders, positions, stops)
        report = await run_current_inventory_completeness_probe(
            ReadOnlyMexcProbe(client),
        )
        self.assertTrue(report["account_wide_completeness_verified"])
        return client, report

    def model(self, client, report, **overrides):
        return assess_shared_cross_pilot_precheck(
            positions=client.positions, stops=client.stops,
            inventory=report,
            proposed_intent=overrides.pop("proposed_intent", next_intent()),
            **overrides,
        )

    async def test_two_manual_cross_positions_with_full_stops_research_only(self):
        client, report = await self.setup_account()
        r = self.model(client, report)
        self.assertEqual(
            r.status, "SHARED_CROSS_MANUAL_POSITIONS_ACCOUNTED_NONAUTHORIZING",
        )
        self.assertEqual(r.existing_account_positions, 2)
        self.assertEqual(r.existing_positions_without_observed_full_volume_stop, 0)
        self.assertEqual(r.existing_positions_with_unverified_cross_mode, 0)
        self.assertTrue(r.includes_manual_positions_in_collateral_risk)
        self.assertFalse(r.manual_positions_adopted_by_t100)
        self.assertLess(D(r.modeled_new_trade_stop_loss_usdt), D("50"))
        self.assertFalse(r.cross_margin_liquidation_certified)
        self.assertFalse(r.native_stop_execution_certified)
        self.assertFalse(r.realized_loss_capped_at_budget)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)

    async def test_two_manual_positions_one_tp_only_always_blocks(self):
        client, report = await self.setup_account()
        client.stops[0]["stopLossPrice"] = "0"
        r = self.model(client, report)
        self.assertEqual(
            r.status, "SHARED_CROSS_MANUAL_POSITION_STOP_NOT_OBSERVED",
        )
        self.assertEqual(r.existing_positions_without_observed_full_volume_stop, 1)

    async def test_two_manual_positions_no_stops_both_block(self):
        client, report = await self.setup_account()
        client.stops = []
        # Never trust stale count/completeness when the observed data changed.
        r = self.model(client, report)
        self.assertEqual(r.status, "SHARED_CROSS_ACCOUNT_INVENTORY_UNVERIFIED")

    async def test_tiny_or_partial_stops_do_not_cover_position(self):
        client, report = await self.setup_account()
        client.stops[0]["vol"] = "0.1"
        r = self.model(client, report)
        self.assertEqual(r.status, "SHARED_CROSS_MANUAL_POSITION_STOP_NOT_OBSERVED")
        self.assertEqual(r.existing_positions_without_observed_full_volume_stop, 1)

    async def test_cross_only_account_cannot_silently_include_isolated_manual(self):
        client, report = await self.setup_account()
        client.positions[0]["openType"] = 1
        r = self.model(client, report)
        self.assertEqual(
            r.status, "SHARED_CROSS_EXISTING_MARGIN_MODES_NOT_ALL_CROSS",
        )
        self.assertEqual(r.existing_positions_with_unverified_cross_mode, 1)

    async def test_same_symbol_as_manual_position_blocks_entry_proposal(self):
        client, report = await self.setup_account()
        r = self.model(
            client, report,
            proposed_intent=next_intent(symbol=client.positions[0]["symbol"]),
        )
        self.assertEqual(r.status, "SHARED_CROSS_CANDIDATE_SYMBOL_ALREADY_OPEN")

    async def test_more_than_three_manual_positions_blocks_four_slot_pilot(self):
        client, report = await self.setup_account(n=4)
        r = self.model(client, report)
        self.assertEqual(r.status, "SHARED_CROSS_ACCOUNT_SLOT_LIMIT")

    async def test_large_notional_exceeds_fifty_dollar_modeled_stop_budget(self):
        client, report = await self.setup_account()
        r = self.model(
            client, report, proposed_intent=next_intent("100"),
        )
        self.assertEqual(r.status, "SHARED_CROSS_PILOT_STOP_EXCEEDS_BUDGET")
        self.assertGreater(D(r.modeled_new_trade_stop_loss_usdt), D("50"))
        self.assertFalse(r.live_authorization)

    async def test_invalid_budget_and_missing_margin_evidence_block(self):
        client, report = await self.setup_account()
        for bad in (D("0"), D("51"), D("NaN")):
            with self.subTest(bad=str(bad)):
                self.assertEqual(self.model(
                    client, report, loss_budget_usdt=bad,
                ).status, "SHARED_CROSS_PRECHECK_INPUT_INVALID")
        changed = {**report, "account_wide_completeness_verified": False}
        self.assertEqual(
            self.model(client, changed).status,
            "SHARED_CROSS_ACCOUNT_INVENTORY_UNVERIFIED",
        )

    async def test_unmatched_stop_or_order_state_blocks(self):
        client, report = await self.setup_account()
        client.stops[0]["positionId"] = "orphan-order-position"
        self.assertEqual(
            self.model(client, report).status,
            "SHARED_CROSS_TPSL_IDENTITY_OR_STATE_UNVERIFIED",
        )
        client, report = await self.setup_account()
        client.stops[0]["state"] = 2
        self.assertEqual(
            self.model(client, report).status,
            "SHARED_CROSS_TPSL_IDENTITY_OR_STATE_UNVERIFIED",
        )

    def test_no_live_transport_in_policy(self):
        import inspect
        import app.t100_live_shared_cross_pilot_precheck as m
        source = inspect.getsource(m)
        for forbidden in (
            "._private_post(", "submit_short_with_stop(",
            "PostgresProtectedV40Commit(", "LiveMutationPermit",
            "conn.execute(", "pool.execute(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
