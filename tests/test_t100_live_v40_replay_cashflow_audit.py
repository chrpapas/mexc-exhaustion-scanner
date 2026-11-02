"""Independent exported cashflow arithmetic rejects corrupt chronology and PnL."""
import copy
import unittest
from scripts.v40_replay_cashflow_audit import audit_rows


class CashflowAuditTests(unittest.TestCase):
    def fixture(self):
        entries = [{'symbol': 'TEST_USDT', 'time': '2026-01-01T00:00:00+00:00', 'notional': '100'}]
        exits = [{'symbol': 'TEST_USDT', 'entry_time': entries[0]['time'],
                  'exit_time': '2026-01-01T00:30:00+00:00', 'notional': '100', 'kind': 'normal_exit',
                  'entry': '10', 'exit': '9', 'pnl_net_before_funding': '9.028'}]
        fills = [{'filled_usdt': '100'}, {'filled_usdt': '90'}]
        metric = {'entered': '1', 'open_positions': '0', 'fee_and_slip': '2.052'}
        return entries, exits, fills, metric

    def test_independent_short_arithmetic_and_closed_slot(self):
        result = audit_rows(*self.fixture(), 100)
        self.assertEqual(result['peak_slots'], 1)
        self.assertEqual(result['final_open_slots'], 0)
        self.assertFalse(result['final_equity_independently_verified'])
        self.assertFalse(result['live_authorization'])

    def test_corrupt_fee_pnl_origin_and_future_entry_fail(self):
        for field, value in (('pnl_net_before_funding', '10'), ('entry_time', '2026-01-02T00:00:00+00:00'),
                             ('symbol', 'FOREIGN_USDT'), ('exit', 'NaN')):
            with self.subTest(field=field):
                entries, exits, fills, metric = self.fixture()
                exits[0][field] = value
                with self.assertRaises(ValueError):
                    audit_rows(entries, exits, fills, metric, 100)
        entries, exits, fills, metric = self.fixture()
        metric['fee_and_slip'] = '0'
        with self.assertRaisesRegex(ValueError, 'FEE_AND_SLIPPAGE_MISMATCH'):
            audit_rows(entries, exits, fills, metric, 100)

    def test_duplicate_symbol_and_fifth_slot_fail(self):
        for unique in (False, True):
            entries, exits, fills, metric = self.fixture()
            entries = [dict(entries[0], symbol=f'TEST{i}_USDT' if unique else 'TEST_USDT') for i in range(5)]
            metric['entered'] = '5'
            with self.assertRaisesRegex(ValueError, 'FOUR_SLOT_LIMIT_EXCEEDED' if unique else 'DUPLICATE_OPEN_SYMBOL'):
                audit_rows(entries, exits, fills, metric, 100)

    def test_repeated_exit_cannot_spend_closed_position(self):
        entries, exits, fills, metric = self.fixture()
        exits.append(copy.deepcopy(exits[0]))
        with self.assertRaisesRegex(ValueError, 'EXIT_ORIGIN_MISMATCH'):
            audit_rows(entries, exits, fills, metric, 100)

    def test_same_bar_normal_exit_does_not_free_symbol_for_admission(self):
        entries, exits, fills, metric = self.fixture()
        entries.append(dict(entries[0], time=exits[0]['exit_time']))
        metric['entered'] = '2'
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_OPEN_SYMBOL'):
            audit_rows(entries, exits, fills, metric, 100)
