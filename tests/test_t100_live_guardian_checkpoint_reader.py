"""Offline adversarial checkpoint provenance tests. No network/DB writes."""
from __future__ import annotations

from decimal import Decimal as D
import hashlib
import unittest

from app.t100_live_guardian_checkpoint_reader import (
    GuardianCheckpointHalted,
    check_api_scope,
    load_pinned_owned_checkpoint,
)


KEY = "testing-api-key-1234567"
SCOPE = hashlib.sha256(KEY.encode()).hexdigest()
OID = "t100v4_abcdef012345_entry"


def cp(**changes):
    r=dict(
        symbol="BTC_USDT", external_oid=OID, exchange_order_id="666",
        position_id="555", contracts=D("1"), entry_price=D("73000"),
        stop_order_id="777", stop_price=D("74500"),
        best_profit_pct=D("0"), armed=False,
        lifecycle_state="OPEN", checkpoint_version=1,
    )
    r.update(changes)
    return r


def revision(**changes):
    r=dict(
        position_id="555", checkpoint_version=1, write_reason="ENTRY_PROTECTED",
        symbol="BTC_USDT",external_oid=OID,exchange_order_id="666",
        contracts=D("1"),entry_price=D("73000"),stop_order_id="777",
        stop_price=D("74500"),best_profit_pct=D("0"),armed=False,
        lifecycle_state="OPEN",
    )
    r.update(changes)
    return r


def enrollment(**changes):
    r=dict(scope_sha256=SCOPE, position_id="555", symbol="BTC_USDT",
           external_oid=OID, exchange_order_id="666")
    r.update(changes)
    return r


def intent(**changes):
    r=dict(symbol="BTC_USDT",external_oid=OID,exchange_order_id="666",
           volume_contracts=D("1"),stop_price=D("74500"),state="ACK")
    r.update(changes)
    return r


def amendment(**changes):
    r=dict(external_id="t100stop_abc", symbol="BTC_USDT",
           position_id="555",stop_order_id="777",
           verified_stop_order_id="777",previous_stop=D("74500"),
           proposed_stop=D("74000"),state="PREPARED")
    r.update(changes)
    return r


class FakeDb:
    def __init__(self, *, cps=None, revs=None, intents=None, amendments=None,
                 enrollments=None, mode="on", bad_priv=None, raise_fetch=False):
        self.cps = [cp()] if cps is None else cps
        self.revs = [revision()] if revs is None else revs
        self.intents = [intent()] if intents is None else intents
        self.enrollments = [enrollment()] if enrollments is None else enrollments
        self.amendments = [] if amendments is None else amendments
        self.mode = mode
        self.bad_priv = bad_priv
        self.raise_fetch = raise_fetch
        self.calls=[]

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow",sql,args))
        perms = {key:False for key in (
            "cp_insert","cp_update","cp_delete",
            "intent_update","stop_update",
            "enrollment_insert","enrollment_update","enrollment_delete")}
        if self.bad_priv:
            perms[self.bad_priv] = True
        return dict(read_only=self.mode, enrollment_select=True, **perms)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch",sql,args))
        if self.raise_fetch:
            raise RuntimeError("DB_DOWN")
        if "t100_guardian_account_enrollments" in sql:
            return self.enrollments
        if "t100_live_position_checkpoint_revisions" in sql:
            return self.revs
        if "t100_live_position_checkpoints" in sql:
            return self.cps
        if "t100_live_order_intents" in sql:
            return self.intents
        if "t100_live_stop_amendments" in sql:
            return self.amendments
        raise AssertionError("Unexpected SQL")


async def inspect(db, **kw):
    args=dict(api_key=KEY, pinned_scope_sha256=SCOPE,
              pinned_position_id="555")
    args.update(kw)
    return await load_pinned_owned_checkpoint(db, **args)


class AccountScopeTests(unittest.TestCase):
    def test_key_fingerprint_matches_only_pinned_account(self):
        check_api_scope(api_key=KEY,pinned_scope_sha256=SCOPE)
        with self.assertRaisesRegex(GuardianCheckpointHalted,"MISMATCH"):
            check_api_scope(api_key=KEY+"x",pinned_scope_sha256=SCOPE)
        with self.assertRaises(GuardianCheckpointHalted):
            check_api_scope(api_key="short",pinned_scope_sha256=SCOPE)
        with self.assertRaises(GuardianCheckpointHalted):
            check_api_scope(api_key=KEY,pinned_scope_sha256="bad")


class CheckpointReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_revision_and_intent_prove_readonly_ownership(self):
        db=FakeDb()
        r=await inspect(db)
        self.assertEqual(r.status,"DURABLE_OWNED_CHECKPOINT_PROVENANCE_VERIFIED_READ_ONLY")
        self.assertTrue(r.checkpoint_revisions_validated)
        self.assertTrue(r.entry_intent_ack_verified)
        self.assertTrue(r.stop_amendments_resolved)
        self.assertTrue(r.account_scope_pinned)
        self.assertFalse(r.live_authorization)
        self.assertEqual(r.owned.position_id,"555")
        self.assertEqual(r.owned.scope_sha256,SCOPE)
        self.assertTrue(all(
            kind == "fetchrow" or "SELECT" in statement
            for kind,statement,_ in db.calls
        ))

    async def test_absent_checkpoint_never_adopts_exchange_position(self):
        db=FakeDb(cps=[])
        result=await inspect(db)
        self.assertEqual(result.status,"NO_DURABLE_T100_POSITION_RECORDED")
        self.assertIsNone(result.owned)
        self.assertEqual(len(db.calls),2)

    async def test_multiple_active_checkpoints_require_account_mapping(self):
        db=FakeDb(cps=[cp(),cp(position_id="888")])
        with self.assertRaisesRegex(GuardianCheckpointHalted,"MULTIPLE"):
            await inspect(db)

    async def test_explicit_pin_will_not_allow_manual_position_adoption(self):
        db=FakeDb()
        with self.assertRaisesRegex(GuardianCheckpointHalted,"PINNED_POSITION"):
            await inspect(db,pinned_position_id="999")

    async def test_wrong_account_key_aborts_before_database(self):
        db=FakeDb()
        with self.assertRaisesRegex(GuardianCheckpointHalted,"FINGERPRINT"):
            await inspect(db,api_key=KEY+"wrong")
        self.assertEqual(db.calls,[])

    async def test_db_not_readonly_or_mutation_capable_role_is_blocked(self):
        for variant in ("off","cp_insert","cp_update","cp_delete",
                        "intent_update","stop_update",
                        "enrollment_insert","enrollment_update","enrollment_delete"):
            with self.subTest(variant=variant):
                db=(FakeDb(mode="off") if variant=="off"
                    else FakeDb(bad_priv=variant))
                with self.assertRaisesRegex(GuardianCheckpointHalted,"PRIVILEGES"):
                    await inspect(db)
                self.assertEqual(len(db.calls),1)

    async def test_no_durable_enrollment_refuses_unverified_account(self):
        db=FakeDb(enrollments=[])
        with self.assertRaisesRegex(GuardianCheckpointHalted,"ENROLLMENT"):
            await inspect(db)
        self.assertEqual(len(db.calls),3)

    async def test_enrollment_identity_cannot_be_inferred_or_swapped(self):
        for change in ({"scope_sha256":"b"*64},{"position_id":"999"},
                       {"symbol":"ETH_USDT"},{"external_oid":"t100other_abcdefgh"},
                       {"exchange_order_id":"999"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(GuardianCheckpointHalted,"ENROLLMENT"):
                    await inspect(FakeDb(enrollments=[enrollment(**change)]))

    async def test_duplicate_enrollment_is_an_explicit_halt(self):
        with self.assertRaisesRegex(GuardianCheckpointHalted,"ENROLLMENT"):
            await inspect(FakeDb(enrollments=[enrollment(),enrollment()]))

    async def test_corrupted_revision_chain_denied(self):
        for revs in (
            [],
            [revision(write_reason="OTHER")],
            [revision(stop_price=D("76000"))],
            [revision(), revision()],
        ):
            with self.subTest(revs=revs):
                with self.assertRaises(GuardianCheckpointHalted):
                    await inspect(FakeDb(revs=revs))

    async def test_uncertified_entry_intent_is_denied(self):
        for intents in (
            [],
            [intent(state="PREPARED")],
            [intent(exchange_order_id="0")],
            [intent(stop_price=D("73000"))],
        ):
            with self.subTest(intents=intents):
                with self.assertRaises(GuardianCheckpointHalted):
                    await inspect(FakeDb(intents=intents))

    async def test_unresolved_stop_amendment_is_denied(self):
        with self.assertRaises(GuardianCheckpointHalted):
            await inspect(FakeDb(amendments=[amendment()]))

    async def test_closed_or_review_checkpoint_cannot_be_observed_as_open(self):
        for state in ("CLOSED","REVIEW","CLOSING"):
            with self.subTest(state=state):
                with self.assertRaises(GuardianCheckpointHalted):
                    await inspect(FakeDb(cps=[cp(lifecycle_state=state)]))

    async def test_query_errors_halt_without_mutations(self):
        db=FakeDb(raise_fetch=True)
        with self.assertRaisesRegex(GuardianCheckpointHalted,"LEDGER"):
            await inspect(db)
        self.assertEqual(len(db.calls),2)

    async def test_raw_ids_and_secrets_never_in_report(self):
        r=await inspect(FakeDb())
        self.assertNotIn(KEY,str(r))
        self.assertFalse(r.live_authorization)


if __name__ == "__main__":
    unittest.main()
