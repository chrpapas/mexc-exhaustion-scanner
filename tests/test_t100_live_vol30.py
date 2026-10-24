import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.t100_live_vol30 import Vol30Parent, Vol30MarketSnapshot, Vol30H4Planner

NOW = datetime(2026, 10, 23, 14, 0, 1, tzinfo=timezone.utc)


def parent(**changes):
    p = Vol30Parent(777, 'SIREN_USDT', 'HIGH_225', NOW - timedelta(minutes=5), D('10000'),
                    D('5625'), D('0'), D('0'), 0)
    return replace(p, **changes)


def snapshot(**changes):
    s = Vol30MarketSnapshot(
        observed_at=NOW, book_exchange_at=NOW-timedelta(milliseconds=300),
        book_received_at=NOW-timedelta(milliseconds=100),
        turnover_window_end=NOW.replace(minute=0, second=0, microsecond=0),
        prior30_turnover_usdt=D('20000'), bucket_committed_notional_usdt=D('0'),
        reference_price=D('2'), best_ask_price=D('2.001'), contract_size=D('1'), contract_step=D('1'),
        min_contracts=D('1'), max_contracts=D('10000'),
        bids=[['2.00','10000'], ['1.99','10000']],
        available_margin_usdt=D('10000'), account_equity_usdt=D('10000'),
        gross_exposure_usdt=D('0'), daily_loss_usdt=D('0'),
        max_gross_exposure_usdt=D('10000'), max_child_notional_usdt=D('10000'))
    return replace(s, **changes)


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.planner = Vol30H4Planner(daily_loss_limit_usdt=D('100'))

    def test_vol30_caps_one_bucket_to_five_percent(self):
        d=self.planner.decide(parent(),snapshot())
        self.assertEqual(d.action,'SUBMIT_CHILD')
        self.assertEqual(d.notional_usdt,D('1000'))
        self.assertEqual(d.contracts,D('500'))

    def test_second_child_same_bucket_uses_reservations(self):
        d=self.planner.decide(parent(pending_notional_usdt=D('600')),
                              snapshot(bucket_committed_notional_usdt=D('600')))
        self.assertEqual(d.notional_usdt,D('400'))

    def test_no_more_orders_after_window_cap(self):
        d=self.planner.decide(parent(),snapshot(bucket_committed_notional_usdt=D('1000')))
        self.assertEqual(d.reason,'VOL30_PARTICIPATION_EXHAUSTED')

    def test_h4_expires_and_does_not_submit(self):
        d=self.planner.decide(parent(admitted_at=NOW-timedelta(minutes=120)),snapshot())
        self.assertEqual(d.action,'EXPIRED')

    def test_dynamic_equity_margin_constraint(self):
        d=self.planner.decide(parent(),snapshot(available_margin_usdt=D('300')))
        self.assertEqual(d.notional_usdt,D('300'))

    def test_dynamic_portfolio_constraint(self):
        d=self.planner.decide(parent(),snapshot(gross_exposure_usdt=D('9900')))
        self.assertEqual(d.notional_usdt,D('100'))

    def test_stale_market_book_blocked(self):
        d=self.planner.decide(parent(),snapshot(book_exchange_at=NOW-timedelta(seconds=3)))
        self.assertEqual(d.reason,'ORDER_BOOK_STALE')

    def test_missing_completed_turnover_blocked(self):
        d=self.planner.decide(parent(),snapshot(turnover_window_end=NOW-timedelta(minutes=30)))
        self.assertEqual(d.reason,'NO_FRESH_PREVIOUS_COMPLETED_30M_TURNOVER')

    def test_halfhour_bucket_validation(self):
        d=self.planner.decide(parent(),snapshot(turnover_window_end=NOW.replace(minute=5)))
        self.assertEqual(d.reason,'NO_FRESH_PREVIOUS_COMPLETED_30M_TURNOVER')

    def test_book_slippage_reduces_child(self):
        d=self.planner.decide(parent(),snapshot(bids=[['2','100'], ['1.9','10000']]))
        self.assertEqual(d.action,'SUBMIT_CHILD')
        self.assertLess(d.notional_usdt,D('1000'))
        self.assertLessEqual(d.adverse_bps,D('100'))

    def test_low_depth_reduces_child(self):
        d=self.planner.decide(parent(),snapshot(bids=[['2','15']]))
        self.assertEqual(d.notional_usdt,D('30'))

    def test_book_invalid_fail_closed(self):
        d=self.planner.decide(parent(),snapshot(bids=[['1.8','10'], ['2','20']]))
        self.assertEqual(d.reason,'UNSORTED_BOOK_BIDS')

    def test_block_when_daily_loss_hit(self):
        d=self.planner.decide(parent(),snapshot(daily_loss_usdt=D('100')))
        self.assertEqual(d.reason,'DAILY_LOSS_KILL_SWITCH')

    def test_no_double_commit_when_target_already_pending(self):
        d=self.planner.decide(parent(pending_notional_usdt=D('5625')),snapshot())
        self.assertEqual(d.action,'FINISHED')

    def test_one_order_does_not_assume_fill(self):
        d=self.planner.decide(parent(),snapshot())
        self.assertEqual(parent().filled_notional_usdt,D('0'))
        self.assertEqual(d.action,'SUBMIT_CHILD')

    def test_invalid_future_quote_fails_closed(self):
        d=self.planner.decide(parent(),snapshot(book_received_at=NOW+timedelta(seconds=1)))
        self.assertEqual(d.reason,'ORDER_BOOK_CLOCK_INVALID')

    def test_no_budget_below_one_contract(self):
        d=self.planner.decide(parent(),snapshot(available_margin_usdt=D('1')))
        self.assertEqual(d.reason,'CONTRACT_MINIMUM_OR_CAP')

    def test_reject_target_above_frozen_tier_fraction(self):
        with self.assertRaisesRegex(ValueError, 'frozen strategy'):
            parent(target_notional_usdt=D('5700'))

    def test_four_slots_blocked(self):
        self.assertEqual(self.planner.decide(parent(other_open_positions=4), snapshot()).reason, 'ALL_FOUR_SLOTS_OCCUPIED')

    def test_spread_too_wide(self):
        self.assertEqual(self.planner.decide(parent(), snapshot(best_ask_price=D('2.01'))).reason, 'SPREAD_EXCEEDS_15_BPS')

    def test_parent_target_is_capped_by_unfilled(self):
        d=self.planner.decide(parent(filled_notional_usdt=D('5425')),
                              snapshot(bucket_committed_notional_usdt=D('500')))
        self.assertEqual(d.notional_usdt,D('200'))

if __name__=='__main__':
    unittest.main()