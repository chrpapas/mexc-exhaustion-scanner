import unittest

from scripts.v40_exit_trigger_audit import audit_position


def exit_row(time, price, reason, kind='normal_exit', amount='100'):
    return {'exit_time': time, 'exit': str(price), 'reason': reason, 'kind': kind, 'notional': amount}


class ExitTriggerAuditTests(unittest.TestCase):
    def test_adverse_stop_precedes_same_bar_trail_activation(self):
        result = audit_position(100, 100, [(0, 180, 190, 80, 90)], [
            exit_row('1970-01-01T00:00:00+00:00', 180, 'pretrail_stop')])
        self.assertEqual(result['triggers'], 1)

    def test_new_low_only_changes_next_bar_stop_and_partial_exit_stays_pending(self):
        result = audit_position(100, 100, [
            (0, 100, 110, 80, 90),
            (1800, 80, 81, 70, 75),
            (3600, 95, 100, 90, 96),
        ], [
            exit_row('1970-01-01T00:30:00+00:00', '80.5', 'trail_partial', amount='40'),
            exit_row('1970-01-01T01:00:00+00:00', 95, 'delayed_forced_exit', 'delayed_exit', '60'),
        ])
        self.assertEqual(result['normal_fills'], 1)
        self.assertEqual(result['delayed_fills'], 1)
        self.assertEqual(result['remaining_notional'], '0')
        self.assertFalse(result['pending'])

    def test_rejects_lookahead_and_invented_delayed_exit(self):
        for row in (
            exit_row('1970-01-01T00:00:00+00:00', '80.5', 'trail'),
            exit_row('1970-01-01T00:00:00+00:00', 100, 'delayed_forced_exit', 'delayed_exit'),
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                audit_position(100, 100, [(0, 100, 110, 80, 90)], [row])

    def test_rejects_optimistic_gap_fill_and_candle_gap(self):
        with self.assertRaisesRegex(ValueError, 'EXIT_PRICE_MISMATCH'):
            audit_position(100, 100, [(0, 180, 190, 90, 185)], [
                exit_row('1970-01-01T00:00:00+00:00', 175, 'pretrail_stop')])
        with self.assertRaisesRegex(ValueError, 'CANDLE_GAP'):
            audit_position(100, 100, [(0, 100, 110, 90, 100), (3600, 100, 110, 90, 100)], [])
