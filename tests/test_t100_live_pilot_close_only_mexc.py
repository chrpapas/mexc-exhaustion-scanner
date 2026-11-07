"""Production close-only MEXC adapter contract, offline no network."""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_owned_emergency_guardian import (
    OwnedGuardianHalted, OwnedShort, close_intent_oid,
    guarded_missing_stop_close,
)
from app.t100_live_pilot_close_only_mexc import PilotCloseOnlyMexcTransport


SCOPE = "b"*64


def owned():
    return OwnedShort(
        scope_sha256=SCOPE, symbol="BTC_USDT", position_id="777",
        entry_external_oid="t100v4_bbbbccccdddd_entry",
        entry_order_id="778", contracts=D("1"),
        stop_order_id="779", stop_price=D("74000"),
        checkpoint_version=1,
    )


class StubMexc:
    def __init__(self, failure=False):
        self.posts = []
        self.failure = failure

    async def _private_post(self, route, body):
        self.posts.append((route, body))
        if self.failure:
            raise TimeoutError("transport outcome unknown")
        return {"orderId": "1000000"}


class MemoryLedger:
    def __init__(self):
        self.st = {}

    async def prepare_if_absent(self, external_oid, position):
        if external_oid in self.st:
            return False
        self.st[external_oid] = "PREPARED"
        return True

    async def transition(self, external_oid, expected, target):
        if self.st.get(external_oid) != expected:
            return False
        self.st[external_oid] = target
        return True


def own_pos():
    return dict(symbol="BTC_USDT", positionId="777", positionType=2,
                state=1, holdVol="1", openType=1, leverage=1)


def manual_pos():
    return dict(symbol="ETH_USDT", positionId="444", positionType=1,
                state=1, holdVol="12", openType=2, leverage=8)


def entry():
    return dict(symbol="BTC_USDT", positionId="777", orderId="778",
                externalOid="t100v4_bbbbccccdddd_entry", side=3,
                state=3, dealVol="1")


def snapshot():
    return dict(account_wide_complete=True,
                positions=[own_pos(),manual_pos()],
                stops=[dict(id="445",symbol="ETH_USDT",positionId="444",state=1)])


class CloseOnlyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_adapter_never_calls_mexc(self):
        mexc = StubMexc()
        transport = PilotCloseOnlyMexcTransport(
            mexc, owned=owned(), account_scope_sha256=SCOPE,
        )
        with self.assertRaisesRegex(OwnedGuardianHalted, "DISABLED"):
            await transport.close_short_once(
                external_oid=close_intent_oid(owned()), symbol="BTC_USDT",
                position_id="777", contracts=D("1"))
        self.assertEqual(mexc.posts, [])

    async def test_exact_v4_real_pilot_order_shape_is_only_mutation(self):
        mexc = StubMexc()
        o = owned()
        transport = PilotCloseOnlyMexcTransport(
            mexc, owned=o, account_scope_sha256=SCOPE,
            emergency_close_enabled=True,
        )
        ack = await transport.close_short_once(
            external_oid=close_intent_oid(o), symbol=o.symbol,
            position_id=o.position_id, contracts=o.contracts)
        self.assertIsInstance(ack,dict)
        self.assertEqual(len(mexc.posts),1)
        route,body = mexc.posts[0]
        self.assertEqual(route,"/api/v1/private/order/create")
        self.assertEqual(body["side"],2)
        self.assertEqual(body["type"],5)
        self.assertEqual(body["openType"],1)
        self.assertEqual(body["vol"],1)
        self.assertEqual(body["positionId"],777)
        self.assertEqual(body["externalOid"],close_intent_oid(o))
        self.assertNotIn("leverage",body)

    async def test_cannot_close_manual_position_even_if_enabled(self):
        mexc = StubMexc()
        o = owned()
        transport = PilotCloseOnlyMexcTransport(
            mexc, owned=o, account_scope_sha256=SCOPE,
            emergency_close_enabled=True,
        )
        for change in (
            {"position_id":"444"}, {"symbol":"ETH_USDT"},
            {"external_oid":"t100gc_"+"a"*22},
            {"contracts":D("2")},
        ):
            with self.subTest(change=change):
                payload=dict(external_oid=close_intent_oid(o),symbol=o.symbol,
                             position_id=o.position_id,contracts=o.contracts)
                payload.update(change)
                with self.assertRaises(OwnedGuardianHalted):
                    await transport.close_short_once(**payload)
        self.assertEqual(mexc.posts, [])

    async def test_one_shot_transport_even_with_duplicate_direct_call(self):
        mexc = StubMexc()
        o = owned()
        t = PilotCloseOnlyMexcTransport(
            mexc,owned=o,account_scope_sha256=SCOPE,
            emergency_close_enabled=True,
        )
        kwargs=dict(external_oid=close_intent_oid(o),symbol=o.symbol,
                    position_id=o.position_id,contracts=o.contracts)
        await t.close_short_once(**kwargs)
        with self.assertRaisesRegex(OwnedGuardianHalted,"RETRY_BLOCKED"):
            await t.close_short_once(**kwargs)
        self.assertEqual(len(mexc.posts),1)

    async def test_transport_scope_btc_and_volume_limited(self):
        from dataclasses import replace
        for o,scope in ((replace(owned(),symbol="ETH_USDT"),SCOPE),
                        (replace(owned(),contracts=D("2")),SCOPE),
                        (owned(),"c"*64)):
            with self.subTest(o=o,scope=scope):
                with self.assertRaises(OwnedGuardianHalted):
                    PilotCloseOnlyMexcTransport(
                        StubMexc(),owned=o,account_scope_sha256=scope,
                        emergency_close_enabled=True,
                    )

    async def test_full_guardian_to_close_only_transport_never_targets_manual(self):
        client=StubMexc()
        o=owned()
        t=PilotCloseOnlyMexcTransport(
            client,owned=o,account_scope_sha256=SCOPE,
            emergency_close_enabled=True,
        )
        ledger=MemoryLedger()
        first=await guarded_missing_stop_close(
            owned=o,actual_scope=SCOPE,
            snapshot_a=snapshot(),snapshot_b=snapshot(),
            entry_order_a=entry(),entry_order_b=entry(),
            ledger=ledger,exchange=t,mutation_enabled=True,
        )
        self.assertTrue(first.post_attempted)
        self.assertFalse(first.account_flat_verified)
        self.assertEqual(first.status,"CLOSE_POST_ACK_UNVERIFIED_RECONCILE_ONLY")
        self.assertEqual(len(client.posts),1)
        self.assertEqual(client.posts[0][1]["positionId"],777)
        self.assertNotIn(444,[x[1]["positionId"] for x in client.posts])
        again=await guarded_missing_stop_close(
            owned=o,actual_scope=SCOPE,
            snapshot_a=snapshot(),snapshot_b=snapshot(),
            entry_order_a=entry(),entry_order_b=entry(),
            ledger=ledger,exchange=t,mutation_enabled=True,
        )
        self.assertEqual(again.status,"INTENT_ALREADY_EXISTS_RECONCILE_ONLY")
        self.assertEqual(len(client.posts),1)

    async def test_timeout_marks_journal_unknown_not_retry(self):
        client=StubMexc(failure=True)
        o=owned()
        t=PilotCloseOnlyMexcTransport(
            client,owned=o,account_scope_sha256=SCOPE,
            emergency_close_enabled=True,
        )
        ledger=MemoryLedger()
        result=await guarded_missing_stop_close(
            owned=o,actual_scope=SCOPE,
            snapshot_a=snapshot(),snapshot_b=snapshot(),
            entry_order_a=entry(),entry_order_b=entry(),
            ledger=ledger,exchange=t,mutation_enabled=True,
        )
        self.assertEqual(result.status,"CLOSE_POST_AMBIGUOUS_RECONCILE_ONLY")
        self.assertEqual(list(ledger.st.values()),["UNKNOWN"])
        self.assertEqual(len(client.posts),1)


if __name__=="__main__":
    unittest.main()
