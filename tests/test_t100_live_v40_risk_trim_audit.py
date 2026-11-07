import unittest
from decimal import Decimal as D

from scripts.v40_risk_trim_audit import planned_trims


class RiskTrimAuditTests(unittest.TestCase):
    def test_largest_exposure_first_and_residual_satisfies_headroom(self):
        equity = D(500)
        marked = {'SMALL': D(100), 'LARGE': D(1200)}
        plans = planned_trims(equity, marked, 100)
        self.assertEqual(list(plans), ['LARGE'])
        reduced = plans['LARGE']*marked['LARGE']
        b = D('.2')+D('1.2')/(5*D('.95'))
        self.assertLess(abs(equity*D('.998')-reduced*D('.0108')-b*(1300-reduced)), D('1e-20'))

    def test_sufficient_equity_and_nonpositive_equity(self):
        self.assertEqual(planned_trims(2000, {'A': 100}, 200), {})
        self.assertEqual(planned_trims(0, {'B': 100, 'A': 100}, 200), {'A': D(1), 'B': D(1)})

    def test_invalid_risk_inputs_fail_closed(self):
        for eq, amounts, cost in [('NaN', {}, 100), (100, {'A': -1}, 100), (100, {}, True)]:
            with self.subTest(eq=eq), self.assertRaises(ValueError):
                planned_trims(eq, amounts, cost)
