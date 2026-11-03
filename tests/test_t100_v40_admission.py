"""Deterministic V40 admission parity fixtures; no exchange calls."""
from __future__ import annotations

import math
import unittest
from decimal import Decimal

from app.t100_v40_admission import (
    PriorBar, admission, causal_49_bar_features, shock_budget, lot_quantize_v39,
)

T = (1761208200 // 1800) * 1800


def prior(amount=1000000.):
    return [PriorBar(T-(49-i)*1800, 1000.*math.exp(i*.001),
                     1000.*math.exp(i*.001), amount) for i in range(49)]


def run(at=6., equity=10000., bars=None, used=0., marked=0.):
    return admission(prior_bars=prior() if bars is None else bars,
                     admission_open_ts=T, equity_usdt=equity,
                     marked_existing_usdt=marked, stage1_atr7_pct=at,
                     active_positions=0, already_open_symbol=False,
                     used_same_bar_usdt=used)


class FrozenV40PolicyTests(unittest.TestCase):
    def test_target_high_62p5_percent_before_caps(self):
        r = run(bars=prior(1000000000.))
        self.assertEqual(r.action, "ADMIT")
        self.assertEqual(r.desired_notional_usdt, 6250.)
        self.assertAlmostEqual(r.modeled_notional_usdt, 6250.)
    def test_low_25_percent(self):
        self.assertEqual(run(at=5., bars=prior(1e9)).desired_notional_usdt, 2500.)
    def test_h4_exit_haircut_not_entry_time_window(self):
        history = prior(20000.)
        history = [PriorBar(b.open_ts, b.high, b.close,
                     5000. if 1 <= i <= 6 else b.amount_usdt)
                   for i, b in enumerate(history)]
        r = run(bars=history)
        self.assertAlmostEqual(r.stressed_exit_capacity_usdt, .05*5000*4*.5/1.75)
        self.assertAlmostEqual(r.modeled_notional_usdt, r.stressed_exit_capacity_usdt)
    def test_49_prior_bars_required(self):
        self.assertEqual(run(bars=prior()[:-1]).action, "MISSING")
    def test_no_current_entry_candle_allowed(self):
        history=prior()
        history[-1]=PriorBar(T,history[-1].high,history[-1].close,history[-1].amount_usdt)
        self.assertEqual(run(bars=history).action,"MISSING")
    def test_volatility_30_skip(self):
        b=[PriorBar(T-(49-i)*1800, 1000+(i%2)*500,
                    1000+(i%2)*500,1000000) for i in range(49)]
        self.assertEqual(run(bars=b).action,"SKIP")
    def test_quota_remaining_capped(self):
        self.assertAlmostEqual(run(bars=prior(20000.), used=800.).modeled_notional_usdt,200.)
    def test_four_positions_reject(self):
        r=admission(prior_bars=prior(), admission_open_ts=T,
                    equity_usdt=10000.,marked_existing_usdt=0.,
                    stage1_atr7_pct=6.,active_positions=4,already_open_symbol=False)
        self.assertEqual(r.reason,"four_slot_limit")
    def test_marked_exposure_shock_budget(self):
        b=shock_budget(equity_usdt=10000.,marked_existing_usdt=19000.,cost_bps=100.)
        self.assertGreaterEqual(b,0.)
        self.assertLess(b,6250.)
    def test_lot_count_v39_same_price(self):
        volume,usdt,children=lot_quantize_v39(modeled_notional=1000.05,
                    entry_price=2.,contract_size="1",volume_step="1",
                    min_volume="1",max_volume="100")
        self.assertEqual((volume,usdt,children),(Decimal(500),Decimal(1000),5))


if __name__ == "__main__":
    unittest.main()
