"""Offline tests for guardian incident outbox and dispatcher.

Every venue/DB/webhook is injected and fake. No real services, credentials,
manual-position mutations, MEXC POST, or ledger checkpoint writes.
"""
from __future__ import annotations

import json
import unittest

from app.t100_live_guardian_incident_outbox import (
    GuardianIncidentHalted,GuardianIncidentOutbox,
)
from app.t100_live_guardian_durable_worker import (
    GuardianDurableHalted,durable_config,run,verify_alert_writer_role,
)
from app.t100_live_guardian_readonly_once import ReadOnlyCycleSummary
from tests.test_t100_live_guardian_readonly_worker import worker_env


SCOPE="c"*64
HOOK="https://discord.com/api/webhooks/123456789012345678/"+("a"*40)


def env(**updates):
    row=worker_env(
        T100_GUARDIAN_DURABLE_ALERTS_ENABLED="true",
        T100_GUARDIAN_INCIDENT_DB_DSN="postgresql://guardian-alert@host/ops",
        T100_GUARDIAN_DISCORD_WEBHOOK_URL=HOOK,
    )
    row["T100_GUARDIAN_ACCOUNT_KEY_SHA256"]="c"*64
    # The fake observer is injected; matching KEY hash only checked at
    # config stage. Set test API key hash to its actual value.
    from tests.test_t100_live_guardian_checkpoint_reader import KEY,SCOPE as REAL_SCOPE
    row["T100_GUARDIAN_MEXC_API_KEY"]=KEY
    row["T100_GUARDIAN_ACCOUNT_KEY_SHA256"]=REAL_SCOPE
    row.update(updates)
    return row


class FakeConnection:
    def __init__(self, *, privilege=None, fail=False, unrelated_access=0):
        self.unrelated_access=unrelated_access
        self.events=[]
        self.current=None
        self.revision=0
        self.latch=False
        self.seq=0
        self.closed=False
        self.fail=fail
        self.privilege=privilege

    async def fetchrow(self,sql,*args):
        if self.fail:
            raise RuntimeError("private-db-exception-keep-secret")
        if "has_table_privilege" in sql:
            row=dict(read_only="off",incident_insert=True,
                     incident_update=True,outbox_select=True,
                     outbox_insert=True,outbox_update=True,
                     incident_delete=False,incident_truncate=False,
                     outbox_delete=False,outbox_truncate=False,
                     checkpoint_insert=False,checkpoint_update=False,
                     checkpoint_delete=False,trade_intent_insert=False,
                     stop_update=False,emergency_update=False,
                     enrollment_insert=False,
                     schema_create=False,db_create=False,
                     checkpoint_any=False,revision_any=False,
                     intent_any=False,stop_any=False,
                     emergency_any=False,enrollment_any=False)
            if self.privilege:
                row.update(self.privilege)
            return row
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            scope,status,alert=args
            before=self.current
            was_alert=self.current[1] if self.current else False
            self.current=(status,alert)
            self.revision+=1
            self.latch |= alert
            if alert and before is None or (
                before is not None and
                (before[0]!=status or (alert and not was_alert))
            ):
                self.seq+=1
                self.events.append(dict(
                    seq=self.seq,code=status,
                    severity="critical" if alert else "warning",
                    attempts=0,delivered_at=None,
                ))
            return dict(revision=self.revision,critical_latched=self.latch)
        raise AssertionError("unexpected SQL fetchrow")

    async def fetchval(self,sql,*args):
        if self.fail:
            raise RuntimeError("db secret")
        if "pg_catalog.pg_class" in sql:
            return self.unrelated_access
        if "pg_try_advisory_lock" in sql:
            return True
        if "SET attempts=attempts+1" in sql:
            scope,seq=args
            for event in self.events:
                if event["seq"]==seq and event["delivered_at"] is None:
                    event["attempts"]+=1
                    return event["attempts"]
            return None
        if "SET delivered_at=clock_timestamp()" in sql:
            scope,seq=args
            for event in self.events:
                if event["seq"]==seq and event["delivered_at"] is None:
                    event["delivered_at"]="ACK"
                    return seq
            return None
        if "SELECT count(*)" in sql:
            return sum(x["delivered_at"] is None for x in self.events)
        raise AssertionError("unexpected SQL fetchval")

    async def fetch(self,sql,*args):
        if self.fail:
            raise RuntimeError("db secret")
        if "FROM public.t100_guardian_incident_outbox" in sql:
            return [
                dict(x) for x in self.events
                if x["delivered_at"] is None
            ][:args[1]]
        raise AssertionError("unexpected SQL fetch")

    async def close(self):
        self.closed=True


class FakeNotifier:
    def __init__(self,webhook,cooldown_seconds=0,*,succeed=True):
        self.succeed=succeed
        self.webhook=webhook
        self.sent=[]
        self.closed=False

    async def send(self,notice,*,force=False):
        self.sent.append((notice.key,notice.title,notice.description,notice.severity))
        return self.succeed

    async def close(self):
        self.closed=True


class ConfigTests(unittest.TestCase):
    def test_explicit_durable_config_and_two_distinct_database_roles(self):
        c,dsn,hook,ca=durable_config(env())
        self.assertEqual(dsn,"postgresql://guardian-alert@host/ops")
        self.assertEqual(hook,HOOK)
        self.assertEqual(c.maximum_failures,3)

    def test_any_trading_or_unreviewed_webhook_is_blocked(self):
        for update in (
            dict(T100_GUARDIAN_DURABLE_ALERTS_ENABLED="false"),
            dict(T100_GUARDIAN_WORKER_ENABLED="false"),
            dict(T100_EXECUTION_MODE="live"),
            dict(T100_LIVE_KILL_SWITCH="false"),
            dict(T100_GUARDIAN_INCIDENT_DB_DSN="postgresql://reader@db.example/readonly"),
            dict(T100_GUARDIAN_DISCORD_WEBHOOK_URL="http://discord.com/api/webhooks/123/xxx"),
            dict(T100_GUARDIAN_DISCORD_WEBHOOK_URL="https://evil.example/api/webhooks/123/xxx"),
            dict(T100_GUARDIAN_DISCORD_WEBHOOK_URL=HOOK+"?token=secret"),
            dict(T100_GUARDIAN_DISCORD_WEBHOOK_URL=""),
        ):
            with self.subTest(update=update):
                with self.assertRaises(Exception):
                    durable_config(env(**update))


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_incident_staged_before_delivery_ack(self):
        db=FakeConnection()
        ob=GuardianIncidentOutbox(db,scope_sha256=SCOPE)
        await ob.acquire_singleton()
        item=await ob.observe(status="OBSERVER_SAFETY_ALERT",alert=True)
        self.assertTrue(item.critical_latched)
        self.assertEqual(item.revision,1)
        self.assertEqual(await ob.pending_count(),1)
        pending=await ob.pending()
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0].severity,"critical")
        self.assertEqual(await ob.mark_attempt(pending[0].seq),1)
        self.assertEqual(await ob.pending_count(),1)
        await ob.mark_delivered(pending[0].seq)
        self.assertEqual(await ob.pending_count(),0)

    async def test_healthy_cycle_does_not_clear_previous_critical_latch(self):
        db=FakeConnection()
        ob=GuardianIncidentOutbox(db,scope_sha256=SCOPE)
        a=await ob.observe(status="OBSERVER_SAFETY_ALERT",alert=True)
        b=await ob.observe(status="OBSERVER_CYCLE_OK",alert=False)
        self.assertTrue(a.critical_latched)
        self.assertTrue(b.critical_latched)
        self.assertEqual(b.revision,2)
        self.assertEqual(len(db.events),2) # critical + restoration
        self.assertEqual(db.events[1]["severity"],"warning")

    async def test_repeated_identical_alert_does_not_spam_outbox(self):
        db=FakeConnection()
        ob=GuardianIncidentOutbox(db,scope_sha256=SCOPE)
        for _ in range(5):
            await ob.observe(status="OBSERVER_SAFETY_ALERT",alert=True)
        self.assertEqual(len(db.events),1)
        self.assertEqual(db.revision,5)

    async def test_bad_scope_codes_and_sequences_denied(self):
        with self.assertRaises(GuardianIncidentHalted):
            GuardianIncidentOutbox(FakeConnection(),scope_sha256="wrong")
        ob=GuardianIncidentOutbox(FakeConnection(),scope_sha256=SCOPE)
        for bad in ("STOP","OBSERVER_WITH SPACES", "OBSERVER_x","OBSERVER_"):
            with self.subTest(bad=bad):
                with self.assertRaises(GuardianIncidentHalted):
                    await ob.observe(status=bad,alert=True)
        with self.assertRaises(GuardianIncidentHalted):
            await ob.mark_attempt(0)
        with self.assertRaises(GuardianIncidentHalted):
            await ob.pending(0)


class DurableWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self,results,db=None,notifier=None,cycles=1):
        db=db or FakeConnection()
        notifier=notifier or FakeNotifier(HOOK)
        snapshots=list(results)
        seen=[]
        async def observe(_env):
            item=snapshots.pop(0)
            if isinstance(item,Exception):
                raise item
            return item
        async def connect(_dsn,_ca):
            return db
        async def pause(_secs):
            pass
        x=await run(environ=env(),cycles=cycles,connect=connect,
                    observe=observe,pause=pause,emit=seen.append,
                    notifier_factory=lambda *_args,**_kwargs:notifier)
        return x,db,notifier,seen

    async def test_protected_cycle_has_no_incident_and_no_latch(self):
        result,db,notifier,logs=await self.execute([
            ReadOnlyCycleSummary(
                status="OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED",
                managed_position_observed=True,
                native_market_stop_verified=True,alert=False)
        ])
        self.assertEqual(result.status,"OBSERVER_CYCLE_OK")
        self.assertFalse(result.alert)
        self.assertFalse(result.critical_latched)
        self.assertEqual(result.pending,0)
        self.assertEqual(notifier.sent,[])
        self.assertTrue(db.closed)
        self.assertTrue(notifier.closed)
        self.assertFalse(result.live_authorization)
        self.assertEqual(result.mexc_orders_sent,0)
        self.assertNotIn(HOOK,"".join(logs))

    async def test_critical_alert_committed_before_discord_ack(self):
        notification=FakeNotifier(HOOK)
        result,db,notifier,logs=await self.execute([
            ReadOnlyCycleSummary(status="OWNED_STOP_MISSING_TRIGGER_STATUS_UNCERTAIN",
                                 alert=True)
        ],notifier=notification)
        self.assertTrue(result.alert)
        self.assertTrue(result.critical_latched)
        self.assertEqual(result.pending,0)
        self.assertEqual(len(notifier.sent),1)
        self.assertEqual(len(db.events),1)
        self.assertEqual(db.events[0]["delivered_at"],"ACK")
        self.assertEqual(result.delivered_this_cycle,1)
        self.assertEqual(db.events[0]["attempts"],1)

    async def test_webhook_outage_leaves_incident_pending_after_shutdown(self):
        notification=FakeNotifier(HOOK,succeed=False)
        x,db,notify,_=await self.execute([
            ReadOnlyCycleSummary(status="UNKNOWN_EXCHANGE",alert=True),
        ],notifier=notification)
        self.assertTrue(x.alert)
        self.assertEqual(x.pending,1)
        self.assertIsNone(db.events[0]["delivered_at"])
        self.assertEqual(db.events[0]["attempts"],1)

    async def test_last_critical_latch_not_recovered_by_later_healthy_cycle(self):
        healthy=ReadOnlyCycleSummary(
            status="OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED",
            managed_position_observed=True,
            native_market_stop_verified=True,alert=False)
        unknown=ReadOnlyCycleSummary(status="UNKNOWN",alert=True)
        x,db,_,logs=await self.execute([unknown,healthy],cycles=2)
        self.assertTrue(x.critical_latched)
        self.assertTrue(x.alert)
        self.assertEqual(len(db.events),2)
        self.assertEqual(db.current[0],"OBSERVER_CYCLE_OK")
        self.assertNotIn(HOOK,"".join(logs))

    async def test_observer_exception_redacted_and_durable(self):
        secret="very-private-key-in-exception"
        x,db,_,logs=await self.execute([
            RuntimeError(secret),
        ])
        self.assertTrue(x.alert)
        self.assertEqual(x.status,"OBSERVER_SIGNED_GET_OR_DB_UNAVAILABLE")
        self.assertEqual(db.events[0]["code"],x.status)
        self.assertNotIn(secret,"".join(logs))
        self.assertNotIn(secret,str(x))

    async def test_writer_role_with_unsafe_privilege_aborts_before_observation(self):
        for privilege in (
            "checkpoint_insert", "checkpoint_update", "checkpoint_delete",
            "incident_delete", "incident_truncate", "outbox_delete",
            "outbox_truncate", "trade_intent_insert", "stop_update",
            "emergency_update", "enrollment_insert",
            "schema_create", "db_create", "checkpoint_any",
            "revision_any", "intent_any", "stop_any",
            "emergency_any", "enrollment_any",
        ):
            with self.subTest(privilege=privilege):
                db=FakeConnection(privilege={privilege:True})
                called=[]
                async def connect(_dsn,_ca):
                    return db
                async def observe(_env):
                    called.append("should not happen")
                    return ReadOnlyCycleSummary(status="UNKNOWN",alert=True)
                with self.assertRaises(GuardianDurableHalted):
                    await run(environ=env(),cycles=1,connect=connect,
                              observe=observe,
                              notifier_factory=lambda *_a,**_k:FakeNotifier(HOOK))
                self.assertEqual(called,[])
                self.assertEqual(db.events,[])
                self.assertTrue(db.closed)

    async def test_future_public_table_read_grant_halts_before_observation(self):
        db=FakeConnection(unrelated_access=1)
        called=[]
        async def connect(_dsn,_ca):
            return db
        async def observe(_env):
            called.append(True)
            return ReadOnlyCycleSummary(status="UNKNOWN",alert=True)
        notifier=FakeNotifier(HOOK)
        with self.assertRaises(GuardianDurableHalted):
            await run(environ=env(),cycles=1,connect=connect,
                      observe=observe,
                      notifier_factory=lambda *_a,**_k:notifier)
        self.assertEqual(called,[])
        self.assertEqual(notifier.sent,[])
        self.assertTrue(db.closed)

    async def test_outbox_database_outage_aborts_no_untracked_notification(self):
        db=FakeConnection(fail=True)
        async def connect(_dsn,_ca):
            return db
        n=FakeNotifier(HOOK)
        with self.assertRaises(GuardianDurableHalted):
            await run(environ=env(),cycles=1,connect=connect,
                      notifier_factory=lambda *_a,**_k:n)
        self.assertEqual(n.sent,[])
        self.assertTrue(n.closed)


if __name__=="__main__":
    unittest.main()



def test_incident_writer_without_trigger_outbox_insert_privilege_is_denied():
    import asyncio
    from app.t100_live_guardian_durable_worker import verify_alert_writer_role
    missing=FakeConnection(privilege={"outbox_insert":False})
    with unittest.TestCase().assertRaisesRegex(
        GuardianDurableHalted,"UNSAFE_PRIVILEGES"
    ):
        asyncio.run(verify_alert_writer_role(missing))
    good=FakeConnection()
    asyncio.run(verify_alert_writer_role(good))
