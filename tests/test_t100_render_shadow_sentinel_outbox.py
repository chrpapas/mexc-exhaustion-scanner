"""Offline, credential-free durable Render sentinel outbox policy tests."""
from __future__ import annotations

import asyncio
import unittest

from app.t100_render_shadow_alert_outbox import (
    RenderAlertOutboxFailure, RenderShadowAlertOutbox,
)
from app.t100_render_shadow_sentinel_durable import (
    durable_config, run, DurableShadowSentinelCycle,
)
from app.t100_render_shadow_sentinel import RenderSentinelHalted


SCOPE = "c" * 64
GOOD = "SHADOW_WITNESS_RECENT_NONAUTHORIZING"
MISSING = "SHADOW_WITNESS_MISSING"
STALE = "SHADOW_WITNESS_STALE"


class FakeDatabase:
    """Simulates PostgreSQL trigger and committed state, shared across sessions."""
    def __init__(self):
        self.states = {}
        self.items = []
        self.seq = 0
        self.locked = False
        self.fail_at = None
        self.closed_count = 0
        self.witness_rows = {}

    def session(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, shared):
        self.db = shared
        self.holds_lock = False
        self.closed = False

    def maybe_fail(self, kind):
        if self.db.fail_at == kind:
            raise RuntimeError("LEAK_ME_EXCHANGE_PRIVATE_SECRET")

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            self.maybe_fail("lease")
            if self.db.locked:
                return False
            self.db.locked = True
            self.holds_lock = True
            return True
        if "INSERT INTO public.t100_shadow_sentinel_state" in sql:
            self.maybe_fail("stage")
            scope, code, alert = args
            prev = self.db.states.get(scope)
            is_new = prev is None
            revision = 1 if is_new else prev["revision"] + 1
            emit = alert and (is_new or not prev["alert"] or prev["code"] != code)
            self.db.states[scope] = dict(code=code, alert=alert, revision=revision)
            if emit:
                self.db.seq += 1
                self.db.items.append(dict(
                    seq=self.db.seq, scope=scope, code=code,
                    attempts=0, delivered=False,
                ))
            return revision
        if "attempts=attempts+1" in sql:
            self.maybe_fail("attempt")
            scope, seq = args
            for item in self.db.items:
                if item["seq"] == seq and item["scope"] == scope and not item["delivered"]:
                    item["attempts"] += 1
                    return item["attempts"]
            return None
        if "delivered_at=clock_timestamp()" in sql:
            self.maybe_fail("ack")
            scope, seq = args
            for item in self.db.items:
                if item["seq"] == seq and item["scope"] == scope and not item["delivered"]:
                    item["delivered"] = True
                    return seq
            return None
        if "SELECT count(*)" in sql:
            self.maybe_fail("count")
            scope = args[0]
            return len([i for i in self.db.items if i["scope"] == scope and not i["delivered"]])
        raise AssertionError("UNEXPECTED_FAKE_SQL")

    async def fetch(self, sql, *args):
        if "FROM public.t100_shadow_sentinel_outbox" not in sql:
            raise AssertionError("UNEXPECTED_FAKE_SQL")
        self.maybe_fail("pending")
        scope, limit = args
        return [
            dict(seq=i["seq"], code=i["code"], attempts=i["attempts"])
            for i in self.db.items if i["scope"] == scope and not i["delivered"]
        ][:limit]

    async def fetchrow(self, sql, *args):
        self.maybe_fail("witness")
        return self.db.witness_rows.get(args[0])

    async def close(self):
        self.closed = True
        self.db.closed_count += 1
        if self.holds_lock:
            self.db.locked = False
            self.holds_lock = False


class FakeNotifier:
    def __init__(self, success=True):
        self.success = success
        self.sent = []
        self.closed = False

    async def send(self, notice, *, force=False):
        self.sent.append((notice.description, force))
        if self.success == "raise":
            raise RuntimeError("URL_SECRET_SHOULD_NOT_LEAK")
        return self.success

    async def close(self):
        self.closed = True


def cfg(**updates):
    env = dict(
        T100_EXECUTION_MODE="paper",
        T100_LIVE_ORDER_API_ENABLED="false",
        T100_LIVE_KILL_SWITCH="true",
        T100_RENDER_SENTINEL_ENABLED="true",
        T100_RENDER_SENTINEL_DURABLE_OUTBOX_ENABLED="true",
        T100_RENDER_SHADOW_SCOPE_SHA256=SCOPE,
        T100_RENDER_SENTINEL_DATABASE_URL="postgresql://fake-local-only",
        T100_RENDER_SENTINEL_DISCORD_WEBHOOK_URL=(
            "https://discord.com/api/webhooks/12345/secret-fake-placeholder"
        ),
    )
    env.update(updates)
    return env


class OutboxOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_alarm_created_once_then_replay_on_restart(self):
        db = FakeDatabase()
        first = db.session()
        outbox = RenderShadowAlertOutbox(first, scope_sha256=SCOPE)
        await outbox.acquire_singleton()
        self.assertEqual(await outbox.observe(code=MISSING, alert=True), 1)
        self.assertEqual(await outbox.observe(code=MISSING, alert=True), 2)
        pending = await outbox.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].code, MISSING)
        self.assertEqual(await outbox.mark_attempt(pending[0].seq), 1)
        await first.close()

        second = db.session()
        resumed = RenderShadowAlertOutbox(second, scope_sha256=SCOPE)
        await resumed.acquire_singleton()
        self.assertEqual(len(await resumed.pending()), 1)
        await resumed.observe(code=MISSING, alert=True)
        self.assertEqual(len(await resumed.pending()), 1)
        await resumed.mark_attempt(pending[0].seq)
        await resumed.mark_delivered(pending[0].seq)
        self.assertEqual(await resumed.count_pending(), 0)
        self.assertEqual(db.items[0]["attempts"], 2)
        self.assertEqual(len(db.items), 1)
        await second.close()

    async def test_healthy_recovery_then_new_alarm_creates_new_event(self):
        db = FakeDatabase()
        conn = db.session()
        journal = RenderShadowAlertOutbox(conn, scope_sha256=SCOPE)
        await journal.acquire_singleton()
        await journal.observe(code=MISSING, alert=True)
        await journal.observe(code=GOOD, alert=False)
        await journal.observe(code=GOOD, alert=False)
        await journal.observe(code=STALE, alert=True)
        self.assertEqual([x.code for x in await journal.pending()], [MISSING, STALE])
        await conn.close()

    async def test_duplicate_concurrent_sentinel_fails_closed(self):
        db = FakeDatabase()
        a, b = db.session(), db.session()
        await RenderShadowAlertOutbox(a, scope_sha256=SCOPE).acquire_singleton()
        with self.assertRaisesRegex(RenderAlertOutboxFailure, "SENTINEL_ALREADY_RUNNING"):
            await RenderShadowAlertOutbox(b, scope_sha256=SCOPE).acquire_singleton()
        await a.close()
        await RenderShadowAlertOutbox(b, scope_sha256=SCOPE).acquire_singleton()
        await b.close()

    async def test_invalid_status_scope_or_boolean_refused(self):
        db = FakeDatabase()
        with self.assertRaisesRegex(RenderAlertOutboxFailure, "SCOPE_INVALID"):
            RenderShadowAlertOutbox(db.session(), scope_sha256="not-valid")
        outbox = RenderShadowAlertOutbox(db.session(), scope_sha256=SCOPE)
        for code, alert in [
            ("PLACE_ORDER", True), ("SHADOW_WITNESS_BAD;DROP", True),
            ("SHADOW_WITNESS_MISSING", 1),
        ]:
            with self.subTest(code=code, alert=alert):
                with self.assertRaisesRegex(RenderAlertOutboxFailure, "STATUS_INVALID"):
                    await outbox.observe(code=code, alert=alert)
        self.assertEqual(db.items, [])

    async def test_db_failed_stage_does_not_claim_alert(self):
        db = FakeDatabase()
        conn = db.session()
        outbox = RenderShadowAlertOutbox(conn, scope_sha256=SCOPE)
        db.fail_at = "stage"
        with self.assertRaises( RenderAlertOutboxFailure) as caught:
            await outbox.observe(code=MISSING, alert=True)
        self.assertEqual(str(caught.exception), "RENDER_OUTBOX_STAGE_FAILED")
        self.assertEqual(len(db.items), 0)
        self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
        await conn.close()

    async def test_ack_failure_keeps_alarm_pending(self):
        db = FakeDatabase()
        conn = db.session()
        outbox = RenderShadowAlertOutbox(conn, scope_sha256=SCOPE)
        await outbox.observe(code=MISSING, alert=True)
        seq = (await outbox.pending())[0].seq
        await outbox.mark_attempt(seq)
        db.fail_at = "ack"
        with self.assertRaisesRegex(RenderAlertOutboxFailure, "ACK_WRITE_FAILED"):
            await outbox.mark_delivered(seq)
        self.assertFalse(db.items[0]["delivered"])
        await conn.close()

    async def test_successful_runner_ack_after_persisted_alarm(self):
        db = FakeDatabase()
        notifier = FakeNotifier(True)
        result = await run(
            environ=cfg(), cycles=1,
            connect=lambda _dsn:_return(db.session()),
            notifier_factory=lambda *_args, **_kw:notifier,
        )
        self.assertEqual(result.status, MISSING)
        self.assertTrue(result.alarm)
        self.assertEqual(result.delivered_this_cycle, 1)
        self.assertEqual(result.pending, 0)
        self.assertTrue(notifier.closed)
        self.assertEqual(len(db.items), 1)
        self.assertTrue(db.items[0]["delivered"])
        self.assertFalse(result.live_authorization)

    async def test_runner_failed_delivery_retries_after_process_restart(self):
        db = FakeDatabase()
        failed = FakeNotifier(False)
        result = await run(
            environ=cfg(), cycles=1,
            connect=lambda _dsn:_return(db.session()),
            notifier_factory=lambda *_args, **_kw:failed,
        )
        self.assertEqual(result.pending, 1)
        self.assertEqual(len(db.items), 1)
        self.assertEqual(db.items[0]["attempts"], 1)
        recovered = FakeNotifier(True)
        retried = await run(
            environ=cfg(), cycles=1,
            connect=lambda _dsn:_return(db.session()),
            notifier_factory=lambda *_args, **_kw:recovered,
        )
        self.assertEqual(retried.pending, 0)
        self.assertEqual(retried.delivered_this_cycle, 1)
        self.assertEqual(db.items[0]["attempts"], 2)
        self.assertEqual(len(db.items), 1)

    async def test_runner_notifier_exception_never_leaks_secret_and_retries(self):
        db = FakeDatabase()
        bad = FakeNotifier("raise")
        result = await run(
            environ=cfg(), cycles=1,
            connect=lambda _dsn:_return(db.session()),
            notifier_factory=lambda *_args, **_kw:bad,
        )
        self.assertEqual(result.pending, 1)
        self.assertEqual(len(db.items), 1)

    async def test_missing_durable_flag_prevents_db_connection(self):
        db = FakeDatabase()
        calls = []
        async def connect(_dsn):
            calls.append(1)
            return db.session()
        with self.assertRaisesRegex(RenderSentinelHalted, "DURABLE_OUTBOX_REQUIRED"):
            await run(
                environ=cfg(T100_RENDER_SENTINEL_DURABLE_OUTBOX_ENABLED="false"),
                cycles=1, connect=connect,
                notifier_factory=lambda *_args,**_kw:FakeNotifier(),
            )
        self.assertEqual(calls, [])

    async def test_mexc_credentials_or_live_mode_refused(self):
        for change in [
            {"MEXC_API_KEY": "not-allowed"},
            {"T100_EXECUTION_MODE": "live"},
            {"T100_LIVE_ORDER_API_ENABLED": "true"},
        ]:
            with self.subTest(change=change):
                with self.assertRaises(RenderSentinelHalted):
                    durable_config(cfg(**change))


async def _return(v):
    return v


if __name__ == "__main__":
    unittest.main()
