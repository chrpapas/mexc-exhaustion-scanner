"""Offline ownership/isolation/once-only POST policy regressions.

No MEXC, Render, database or network calls.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import unittest

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, OwnedGuardianHalted, assess, close_intent_oid,
    guarded_missing_stop_close,
)


SCOPE = "a" * 64


def owned(**kw):
    x = OwnedShort(
        scope_sha256=SCOPE, symbol="BTC_USDT", position_id="111",
        entry_external_oid="t100v4_abcdefgh1234_entry", entry_order_id="112",
        contracts=D("1"), stop_order_id="113", stop_price=D("74000"),
        checkpoint_version=1,
    )
    return replace(x, **kw)


def entry(**kw):
    x = dict(symbol="BTC_USDT", positionId="111", orderId="112",
             externalOid="t100v4_abcdefgh1234_entry",
             side=3, state=3, dealVol="1")
    x.update(kw)
    return x


def position(**kw):
    x = dict(symbol="BTC_USDT", positionId="111",
             positionType=2, state=1, holdVol="1", openType=1)
    x.update(kw)
    return x


def stop(**kw):
    x = dict(id="113", symbol="BTC_USDT", positionId="111",
             positionType=2, state=1, isFinished=0, orderId=0,
             stopLossType=0, volType=2, stopLossReverse=2,
             lossTrend=1, triggerSide=0, errorCode=0,
             profitLossVolType="SAME", stopLossPrice="74000")
    x.update(kw)
    return x


def manual_position(**kw):
    x = dict(symbol="ETH_USDT", positionId="222",
             positionType=1, state=1, holdVol="3", openType=2)
    x.update(kw)
    return x


def snap(*, own=True, protected=True, manual=False, **kw):
    x = dict(account_wide_complete=True,
             positions=[position()] if own else [],
             stops=[stop()] if protected else [])
    if manual:
        x["positions"].append(manual_position())
        x["stops"].append(dict(id="224", symbol="ETH_USDT",
                               positionId="222", state=1))
    x.update(kw)
    return x


class Ledger:
    def __init__(self, fail=False, clash=False):
        self.states = {}
        self.fail = fail
        self.clash = clash

    async def prepare_if_absent(self, oid, position):
        if self.fail:
            raise RuntimeError("DB_DOWN")
        if self.clash or oid in self.states:
            return False
        self.states[oid] = "PREPARED"
        return True

    async def transition(self, oid, expected, target):
        if self.fail:
            raise RuntimeError("DB_DOWN")
        if self.states.get(oid) != expected:
            return False
        self.states[oid] = target
        return True


class Exchange:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def close_short_once(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise TimeoutError("ambiguous_POST")
        return {"success": True}


class GuardianReadOnlyTests(unittest.TestCase):
    def test_exact_owned_active_native_market_stop_protected(self):
        x = assess(owned(), actual_scope=SCOPE, snapshot=snap(),
                   entry_order=entry())
        self.assertEqual(x.status, "OWNED_POSITION_NATIVE_STOP_VERIFIED")
        self.assertTrue(x.protected)
        self.assertFalse(x.close_candidate)
        self.assertFalse(x.live_authorization)

    def test_manual_position_is_visible_but_never_adopted(self):
        x = assess(owned(), actual_scope=SCOPE,
                   snapshot=snap(manual=True), entry_order=entry())
        self.assertTrue(x.protected)
        self.assertEqual(x.foreign_position_count, 1)

    def test_missing_owned_stop_can_be_close_candidate_but_not_authorization(self):
        x = assess(owned(), actual_scope=SCOPE, snapshot=snap(protected=False),
                   entry_order=entry())
        self.assertEqual(x.status, "OWNED_NATIVE_STOP_MISSING")
        self.assertTrue(x.close_candidate)
        self.assertFalse(x.live_authorization)

    def test_manual_stop_is_not_assigned_to_owned_position(self):
        x = assess(owned(), actual_scope=SCOPE,
                   snapshot=snap(protected=False, manual=True),
                   entry_order=entry())
        self.assertEqual(x.status, "OWNED_NATIVE_STOP_MISSING")
        self.assertEqual(x.foreign_position_count, 1)

    def test_missing_ownership_or_stale_checkpoint_fails_closed(self):
        for bad in ({"scope_sha256": "b" * 64},
                    {"checkpoint_lifecycle": "CLOSED"},
                    {"entry_external_oid": "manual_trade"},
                    {"stop_order_id": "0"},
                    {"contracts": D("NaN")},
                    {"checkpoint_version": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(OwnedGuardianHalted):
                    assess(owned(**bad), actual_scope=SCOPE,
                           snapshot=snap(protected=False), entry_order=entry())

    def test_wrong_scope_is_refused_even_with_matching_orders(self):
        with self.assertRaises(OwnedGuardianHalted):
            assess(owned(), actual_scope="b"*64, snapshot=snap(), entry_order=entry())

    def test_unknown_manual_position_cannot_be_substituted(self):
        result = assess(owned(), actual_scope=SCOPE,
                        snapshot=snap(own=False, manual=True, protected=False),
                        entry_order=entry())
        self.assertEqual(result.status, "OWNED_POSITION_ABSENT_OR_DUPLICATE_RECONCILE_ONLY")
        self.assertFalse(result.close_candidate)

    def test_missing_completeness_blocks_even_with_owned_missing_stop(self):
        result = assess(owned(), actual_scope=SCOPE,
                        snapshot=snap(protected=False, account_wide_complete=False),
                        entry_order=entry())
        self.assertEqual(result.status, "EXCHANGE_INVENTORY_NOT_CERTIFIED")
        self.assertFalse(result.close_candidate)

    def test_drifted_volume_or_side_blocks(self):
        for p in (position(holdVol="0.5"), position(positionType=1),
                  position(state=2), position(openType=2)):
            with self.subTest(p=p):
                result = assess(owned(), actual_scope=SCOPE,
                                snapshot=snap(positions=[p], protected=False),
                                entry_order=entry())
                self.assertEqual(result.status, "OWNED_POSITION_STATE_OR_VOLUME_DRIFT")

    def test_entry_not_durably_owned_blocks(self):
        for bad in ({"externalOid": "manual"}, {"orderId": "113"},
                    {"dealVol": "0.5"}, {"state": 2}, {"side": 2}):
            with self.subTest(bad=bad):
                r = assess(owned(), actual_scope=SCOPE,
                           snapshot=snap(protected=False), entry_order=entry(**bad))
                self.assertEqual(r.status, "ENTRY_OWNERSHIP_EVIDENCE_UNVERIFIED")

    def test_unrecognized_or_multiple_stops_do_not_trigger_close(self):
        for stops in ([stop(id="999")], [stop(), stop(id="999")],
                      [stop(stopLossType=1)], [stop(stopLossPrice="90000")],
                      [stop(volType=1)]):
            with self.subTest(stops=stops):
                r = assess(owned(), actual_scope=SCOPE,
                           snapshot=snap(stops=stops), entry_order=entry())
                self.assertFalse(r.close_candidate)
                self.assertFalse(r.protected)

    def test_idempotent_close_oid_is_position_specific(self):
        a = close_intent_oid(owned())
        self.assertTrue(a.startswith("t100gc_"))
        self.assertEqual(a, close_intent_oid(owned()))
        self.assertNotEqual(a, close_intent_oid(owned(position_id="333")))


class EmergencyCloseTests(unittest.IsolatedAsyncioTestCase):
    async def run_guard(self, *, first=None, second=None,
                        first_entry=None, second_entry=None, ledger=None,
                        exchange=None, enabled=True):
        a = first if first is not None else snap(protected=False)
        b = second if second is not None else snap(protected=False)
        led = ledger or Ledger()
        ex = exchange or Exchange()
        result = await guarded_missing_stop_close(
            owned=owned(), actual_scope=SCOPE, snapshot_a=a, snapshot_b=b,
            entry_order_a=first_entry if first_entry is not None else entry(),
            entry_order_b=second_entry if second_entry is not None else entry(),
            ledger=led, exchange=ex, mutation_enabled=enabled,
        )
        return result, led, ex

    async def test_disabled_mutation_never_touches_ledger_or_exchange(self):
        outcome, ledger, exchange = await self.run_guard(enabled=False)
        self.assertEqual(outcome.status, "CLOSE_CANDIDATE_GATED_OFF")
        self.assertEqual(ledger.states, {})
        self.assertEqual(exchange.calls, [])

    async def test_single_attempt_claim_before_post_and_ack_not_flat(self):
        result, ledger, ex = await self.run_guard()
        self.assertEqual(result.status, "CLOSE_POST_ACK_UNVERIFIED_RECONCILE_ONLY")
        self.assertTrue(result.post_attempted)
        self.assertFalse(result.account_flat_verified)
        self.assertFalse(result.live_authorization)
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.calls[0]["symbol"], "BTC_USDT")
        self.assertEqual(ex.calls[0]["position_id"], "111")
        self.assertEqual(ex.calls[0]["contracts"], D("1"))
        self.assertEqual(list(ledger.states.values()), ["SUBMITTING"])

    async def test_same_intent_cannot_post_twice_even_after_restart(self):
        ledger, ex = Ledger(), Exchange()
        first, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        second, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        self.assertTrue(first.post_attempted)
        self.assertFalse(second.post_attempted)
        self.assertEqual(second.status, "INTENT_ALREADY_EXISTS_RECONCILE_ONLY")
        self.assertEqual(len(ex.calls), 1)

    async def test_post_timeout_stays_unknown_and_no_blind_retry(self):
        ledger, ex = Ledger(), Exchange(fail=True)
        first, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        second, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        self.assertEqual(first.status, "CLOSE_POST_AMBIGUOUS_RECONCILE_ONLY")
        self.assertEqual(list(ledger.states.values()), ["UNKNOWN"])
        self.assertFalse(second.post_attempted)
        self.assertEqual(len(ex.calls), 1)

    async def test_durable_journal_error_stops_before_post(self):
        ledger, ex = Ledger(fail=True), Exchange()
        result, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        self.assertEqual(result.status, "DURABLE_LEDGER_FAILED_NO_POST")
        self.assertEqual(ex.calls, [])

    async def test_already_claimed_intent_never_retries(self):
        ledger, ex = Ledger(clash=True), Exchange()
        result, _, _ = await self.run_guard(ledger=ledger, exchange=ex)
        self.assertEqual(result.status, "INTENT_ALREADY_EXISTS_RECONCILE_ONLY")
        self.assertFalse(result.post_attempted)
        self.assertEqual(ex.calls, [])

    async def test_price_or_stop_present_cannot_trigger_close(self):
        r, _, ex = await self.run_guard(first=snap(), second=snap())
        self.assertEqual(r.status, "OWNED_MISSING_STOP_NOT_PROVEN")
        self.assertEqual(ex.calls, [])

    async def test_single_snapshot_drift_blocks_close(self):
        r, _, ex = await self.run_guard(second=snap(protected=False, manual=True))
        self.assertEqual(r.status, "TWO_SNAPSHOT_CONSISTENCY_UNVERIFIED")
        self.assertEqual(ex.calls, [])

    async def test_entry_order_changed_between_two_gets_blocks(self):
        r, _, ex = await self.run_guard(second_entry=entry(dealVol="0.5"))
        self.assertEqual(r.status, "OWNED_MISSING_STOP_NOT_PROVEN")
        self.assertEqual(ex.calls, [])

    async def test_manual_positions_never_appear_in_close_arguments(self):
        r, _, ex = await self.run_guard(
            first=snap(protected=False, manual=True),
            second=snap(protected=False, manual=True))
        self.assertTrue(r.post_attempted)
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.calls[0]["position_id"], "111")
        self.assertNotIn("222", str(ex.calls[0]))

    async def test_wrong_scope_denied_even_when_mutation_flag_on(self):
        ex = Exchange()
        with self.assertRaises(OwnedGuardianHalted):
            await guarded_missing_stop_close(
                owned=owned(), actual_scope="b"*64,
                snapshot_a=snap(protected=False),
                snapshot_b=snap(protected=False),
                entry_order_a=entry(), entry_order_b=entry(),
                ledger=Ledger(), exchange=ex, mutation_enabled=True,
            )
        self.assertEqual(ex.calls, [])


if __name__ == "__main__":
    unittest.main()
