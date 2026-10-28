"""Offline four-slot planning tests. No database, credentials or API calls."""
from __future__ import annotations

import math
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_four_slot_planner import plan_four_slot_v40
from app.t100_live_supervisor import ManagedShort
from app.t100_v40_admission import PriorBar


OPEN_TS = (1761208200 // 1800) * 1800


def prior(amount=1_000_000_000):
    return [
        PriorBar(
            OPEN_TS - (49-i)*1800,
            1000 * math.exp(i * .001),
            1000 * math.exp(i * .001),
            float(amount),
        )
        for i in range(49)
    ]


def inventory(n=0, *, stable=True, complete=True, other_orders=0):
    return {
        "account_wide_completeness_verified": complete,
        "critical_inventory_equal_across_witness_window": stable,
        "after_positions": n,
        "after_current_tpsl": n,
        "open_order_counts": {
            "sumCount": n + other_orders,
            "limitOrderCount": other_orders,
            "stopOrderCount": n,
            "planOrderCount": 0,
            "trackOrderCount": 0,
        },
    }


def recovery(n):
    rows = tuple(
        ManagedShort(
            symbol=f"S{i}_USDT",
            position_id=f"pos{i}",
            contracts=D("20"), entry_price=D("100"),
            stop_order_id=f"stop{i}", stop_price=D("175"),
            best_profit_pct=D("0"), armed=False,
        ) for i in range(n)
    )
    return DurableRecoveryReport(
        recovered=rows,
        checkpoint_count=n,
        exchange_position_count=n,
        exchange_stop_count=n,
    )


def plan(n=0, *, rec=None, inv=None, symbol="NEW_USDT", bars=None, **kwargs):
    args = {
        "inventory": inventory(n) if inv is None else inv,
        "durable_recovery": rec,
        "symbol": symbol,
        "prior_bars": prior() if bars is None else bars,
        "admission_open_ts": OPEN_TS,
        "stage1_atr7_pct": 6.0,
        "equity_usdt": 10_000.,
        "marked_existing_usdt": 0.,
    }
    args.update(kwargs)
    return plan_four_slot_v40(**args)


class FourSlotPlannerTests(unittest.TestCase):
    def test_flat_full_v40_high_tier_model_is_62p5_pct(self):
        p = plan()
        self.assertEqual(p.action, "ADMIT")
        self.assertEqual(p.reason, "modeled_single_bar_admission")
        self.assertEqual(p.occupied_slots, 0)
        self.assertEqual(p.desired_notional_usdt, 6250.)
        self.assertEqual(p.modeled_notional_usdt, 6250.)
        self.assertFalse(p.entry_allowed)
        self.assertFalse(p.live_authorization)

    def test_three_owned_positions_preserve_fourth_slot(self):
        p = plan(3, rec=recovery(3))
        self.assertEqual(p.action, "ADMIT")
        self.assertEqual(p.occupied_slots, 3)
        self.assertFalse(p.live_authorization)

    def test_four_managed_slots_reject_new_entry(self):
        p = plan(4, rec=recovery(4))
        self.assertEqual((p.action, p.reason), ("REJECT", "four_slot_limit"))

    def test_fifth_position_blocks_account_model(self):
        p = plan(5, rec=recovery(5))
        self.assertEqual((p.action, p.reason), ("BLOCK", "ACCOUNT_EXCEEDS_FOUR_SLOTS"))

    def test_unowned_nonflat_account_never_plans_entry(self):
        p = plan(1, rec=None)
        self.assertEqual((p.action, p.reason), ("BLOCK", "NONFLAT_DURABLE_RECOVERY_REQUIRED"))

    def test_unowned_nonflat_account_not_adopted_by_boolean(self):
        p = plan(1, rec=True)
        self.assertEqual((p.action, p.reason), ("BLOCK", "NONFLAT_DURABLE_RECOVERY_REQUIRED"))

    def test_one_managed_position_allows_different_symbol(self):
        p = plan(1, rec=recovery(1))
        self.assertEqual(p.action, "ADMIT")
        self.assertEqual(p.occupied_slots, 1)

    def test_duplicate_owned_symbol_is_rejected(self):
        p = plan(1, rec=recovery(1), symbol="S0_USDT")
        self.assertEqual((p.action, p.reason), ("REJECT", "duplicate_symbol"))

    def test_duplicate_recovered_symbols_block(self):
        r = recovery(2)
        duplicate = replace(r.recovered[1], symbol=r.recovered[0].symbol)
        bad = replace(r, recovered=(r.recovered[0], duplicate))
        p = plan(2, rec=bad)
        self.assertEqual(p.reason, "DUPLICATE_OR_INVALID_MANAGED_IDENTITY")

    def test_duplicate_position_identity_blocks(self):
        r = recovery(2)
        bad = replace(r, recovered=(r.recovered[0],
                      replace(r.recovered[1], position_id=r.recovered[0].position_id)))
        self.assertEqual(plan(2, rec=bad).action, "BLOCK")

    def test_empty_account_with_fabricated_recovery_blocks(self):
        self.assertEqual(plan(0, rec=recovery(1)).reason,
                         "FLAT_ACCOUNT_WITH_NONFLAT_RECOVERY_REPORT")

    def test_recovered_count_drift_blocks(self):
        self.assertEqual(plan(2, rec=recovery(1)).reason,
                         "NONFLAT_RECOVERY_REPORT_MISMATCH")

    def test_unrelated_open_limit_order_blocks(self):
        self.assertEqual(plan(inv=inventory(0, other_orders=1)).reason,
                         "UNRELATED_INFLIGHT_ORDERS_PRESENT")

    def test_open_order_total_mismatch_blocks(self):
        i = inventory(1)
        i["open_order_counts"]["sumCount"] = 100
        self.assertEqual(plan(1, rec=recovery(1), inv=i).reason,
                         "ORDER_COUNTS_INCOHERENT")

    def test_no_protective_stop_blocks(self):
        i = inventory(1)
        i["after_current_tpsl"] = 0
        self.assertEqual(plan(1, rec=recovery(1), inv=i).reason,
                         "POSITION_STOP_COUNT_MISMATCH")

    def test_unstable_or_incomplete_inventory_blocks(self):
        self.assertEqual(plan(inv=inventory(stable=False)).action, "BLOCK")
        self.assertEqual(plan(inv=inventory(complete=False)).action, "BLOCK")

    def test_malformed_inventory_count_fails_closed(self):
        i = inventory()
        i["after_positions"] = True
        self.assertEqual(plan(inv=i).reason, "INVENTORY_COUNTS_INVALID")

    def test_low_tier_target_is_25pct(self):
        p = plan(stage1_atr7_pct=5.0)
        self.assertEqual(p.action, "ADMIT")
        self.assertEqual(p.desired_notional_usdt, 2500.)

    def test_volatility_skip_no_entry(self):
        b = [PriorBar(OPEN_TS-(49-i)*1800, 1000+(i%2)*500,
                      1000+(i%2)*500, 1e9) for i in range(49)]
        p = plan(bars=b)
        self.assertEqual((p.action, p.reason), ("SKIP", "vol_ge_30"))

    def test_future_entry_bar_never_used(self):
        b = prior()
        b[-1] = replace(b[-1], open_ts=OPEN_TS)
        p = plan(bars=b)
        self.assertEqual((p.action, p.reason),
                         ("MISSING", "causal_49_bar_history_incomplete"))

    def test_invalid_symbol_fails_closed(self):
        self.assertEqual(plan(symbol="BTC_USDT").action, "BLOCK")
        self.assertEqual(plan(symbol="../unsafe").action, "BLOCK")

    def test_invalid_v40_parameters_fail_closed(self):
        self.assertEqual(plan(equity_usdt=-1).action, "BLOCK")
        self.assertEqual(plan(stage1_atr7_pct=float("nan")).action, "BLOCK")

    def test_model_never_claims_live_authorization(self):
        cases = (
            plan(),
            plan(1),
            plan(3, rec=recovery(3)),
            plan(4, rec=recovery(4)),
            plan(stage1_atr7_pct=5.),
        )
        for p in cases:
            self.assertFalse(p.entry_allowed)
            self.assertFalse(p.live_authorization)
            if p.v40_admission is not None:
                self.assertNotEqual(p.reason, "H4_120_MIN_WINDOW_EXPIRED")


if __name__ == "__main__":
    unittest.main()
