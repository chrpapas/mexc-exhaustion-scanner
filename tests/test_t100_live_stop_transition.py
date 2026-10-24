"""Offline redacted diagnostics for finished MEXC TP/SL transitions."""
import unittest

from app.t100_live_stop_transition import assess_recent_finished_tpsl_transition


def pos(pid=10):
    return {"positionId": pid, "symbol": "SECRET_USDT"}


def hist(**extra):
    row = {
        "positionId": "10",
        "state": 2,
        "triggerSide": 0,
        "isFinished": 1,
        "createTime": 1_000_000,
        "updateTime": 1_000_000,
        "symbol": "SECRET_USDT",
        "stopLossPrice": 999.0,
        "vol": 123,
        "id": 777,
    }
    row.update(extra)
    return row


class StopTransitionTests(unittest.TestCase):
    def test_recent_canceled_transition(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist()], now_ms=1_030_000)
        self.assertEqual(r["latest_terminal_state"], "CANCELED")
        self.assertEqual(r["latest_trigger_side"], "UNTRIGGERED")
        self.assertTrue(r["latest_is_finished"])
        self.assertTrue(r["latest_row_recent"])
        self.assertEqual(r["status"],
                         "RECENT_FINISHED_TPSL_TRANSITION_OBSERVED")

    def test_executed_stop_loss_transition(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(state=3, triggerSide=2)], now_ms=1_030_000)
        self.assertEqual(r["latest_terminal_state"], "EXECUTED")
        self.assertEqual(r["latest_trigger_side"], "STOP_LOSS")
        self.assertTrue(r["transition_semantics_observed"])

    def test_executed_take_profit_transition(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(state=3, triggerSide=1)], now_ms=1_030_000)
        self.assertEqual(r["latest_trigger_side"], "TAKE_PROFIT")

    def test_invalid_and_failed_states_are_named(self):
        self.assertEqual(
            assess_recent_finished_tpsl_transition(
                [pos()], [hist(state=4)], now_ms=1_030_000
            )["latest_terminal_state"],
            "INVALID",
        )
        self.assertEqual(
            assess_recent_finished_tpsl_transition(
                [pos()], [hist(state=5)], now_ms=1_030_000
            )["latest_terminal_state"],
            "EXECUTION_FAILED",
        )

    def test_latest_matching_row_selected_by_update_time(self):
        rows = [
            hist(state=2, updateTime=1_000_000),
            hist(state=3, triggerSide=2, updateTime=1_020_000),
        ]
        r = assess_recent_finished_tpsl_transition(
            [pos()], rows, now_ms=1_030_000)
        self.assertEqual(r["latest_terminal_state"], "EXECUTED")
        self.assertEqual(r["matching_finished_rows_for_observed_positions"], 2)

    def test_unrelated_history_ignored(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(positionId="999")], now_ms=1_030_000)
        self.assertEqual(r["status"], "NO_MATCHING_FINISHED_TPSL")

    def test_old_row_is_not_recent(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(updateTime=1_000_000)],
            now_ms=3_000_000, recent_window_ms=60_000)
        self.assertFalse(r["latest_row_recent"])
        self.assertEqual(r["latest_age_bucket"], "OLDER")

    def test_unknown_state_does_not_verify_transition(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(state=9)], now_ms=1_030_000)
        self.assertFalse(r["transition_semantics_observed"])
        self.assertEqual(r["latest_terminal_state"], "UNKNOWN_TERMINAL_STATE")

    def test_nonfinal_row_does_not_verify_transition(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(isFinished=0)], now_ms=1_030_000)
        self.assertFalse(r["transition_semantics_observed"])

    def test_future_timestamp_flagged(self):
        r = assess_recent_finished_tpsl_transition(
            [pos()], [hist(updateTime=2_000_000)], now_ms=1_000_000)
        self.assertEqual(r["latest_age_bucket"], "FUTURE_TIMESTAMP")
        self.assertFalse(r["latest_row_recent"])

    def test_empty_and_malformed_fail_closed(self):
        self.assertEqual(
            assess_recent_finished_tpsl_transition([], [])["status"],
            "INSUFFICIENT_ROWS",
        )
        self.assertEqual(
            assess_recent_finished_tpsl_transition([None], [])["status"],
            "INVALID_INPUT",
        )

    def test_no_raw_values_leak(self):
        r = assess_recent_finished_tpsl_transition(
            [pos(123456789)],
            [hist(positionId="123456789", symbol="VERY_SECRET_USDT",
                  id=987654321, stopLossPrice=12345.67, vol=999)],
            now_ms=1_030_000,
        )
        text = str(r)
        for secret in ("VERY_SECRET_USDT", "123456789", "987654321",
                       "12345.67", "999"):
            self.assertNotIn(secret, text)
