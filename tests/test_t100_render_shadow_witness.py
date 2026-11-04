"""Fully offline tests for Render PostgreSQL shadow kill witness.

No Render service, database, MEXC connection or Discord POST is touched.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.t100_live_kill import OperationalKillLatch
from app.t100_render_shadow_witness import (
    RenderShadowWitness, ShadowWitnessFailure, parse_record,
)
from app.t100_render_shadow_witness_runtime import (
    WitnessRuntimeHalted, runtime_config, run as run_witness,
)
from app.t100_render_shadow_sentinel import (
    render_sentinel_config, inspect_witness,
    RenderSentinelHalted, run as run_sentinel,
)

SCOPE = "a" * 64
RUN1, RUN2 = "a" * 12, "b" * 12


class FakeConn:
    """Mock PostgreSQL UPSERT with sticky FIRST reason, never clearing kill."""
    def __init__(self, existing=None, fail=None):
        self.row = existing
        self.fail = fail
        self.calls = []
        self.closed = False

    async def fetchval(self, sql, *params):
        self.calls.append((sql, params))
        if self.fail == "lease":
            raise RuntimeError("HIDDEN_DB_CONNECTION")
        if self.fail == "busy":
            return False
        return True

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        if self.fail:
            raise RuntimeError("PROTECTED_DATABASE_CONNECTION_SECRET")
        if sql.lstrip().startswith("SELECT"):
            return self.row
        scope, run_id, idx, state, killed, reason = params
        if self.row is None:
            self.row = dict(
                scope_sha256=scope, revision=1, run_id=run_id,
                cycle_index=idx, state=state, kill_latched=killed,
                kill_reason=reason, age_seconds=0.0,
            )
        else:
            old = self.row
            latched = old["kill_latched"] or killed
            self.row = dict(
                scope_sha256=scope, revision=old["revision"]+1,
                run_id=run_id, cycle_index=idx,
                state="OBSERVING_KILL_LATCHED" if latched else state,
                kill_latched=latched,
                kill_reason=old["kill_reason"] if old["kill_latched"] else reason,
                age_seconds=0.0,
            )
        return self.row

    async def close(self):
        self.closed = True


class FakeRuntime:
    def __init__(self, *, run_id=RUN1, fail=False, kill_reason=None):
        self._run_id = run_id
        self._fail = fail
        self.kill = OperationalKillLatch()
        if kill_reason:
            self.kill.trip(kill_reason)
        self.stop_event = asyncio.Event()
        self.config = SimpleNamespace(cycle_seconds=90)
        self.cycles = 0
        self.notified = 0
        self.closed = False

    def _trip(self, reason):
        if not self.kill.tripped:
            self.kill.trip(reason)

    async def _notify_kill_once(self):
        self.notified += 1

    async def cycle(self):
        self.cycles += 1
        if self._fail:
            raise RuntimeError("SECRET_URL_HIDDEN")
        killed = self.kill.tripped
        return SimpleNamespace(
            status="OBSERVING_KILL_LATCHED" if killed else "OBSERVING_NON_AUTHORIZING",
            kill_tripped=killed, kill_reason=self.kill.first_reason,
        )

    async def close(self):
        self.closed = True


def writer_env(**kwargs):
    env=dict(
        T100_EXECUTION_MODE="paper",
        T100_LIVE_ORDER_API_ENABLED="false",
        T100_LIVE_KILL_SWITCH="true",
        T100_RENDER_SHADOW_WITNESS_ENABLED="true",
        T100_RENDER_SHADOW_SCOPE_SHA256=SCOPE,
        T100_RENDER_SHADOW_WITNESS_DATABASE_URL="postgres://not-connected-example",
    )
    env.update(kwargs)
    return env


def sentinel_env(**kwargs):
    env=dict(
        T100_EXECUTION_MODE="paper",
        T100_LIVE_ORDER_API_ENABLED="false",
        T100_LIVE_KILL_SWITCH="true",
        T100_RENDER_SENTINEL_ENABLED="true",
        T100_RENDER_SHADOW_SCOPE_SHA256=SCOPE,
        T100_RENDER_SENTINEL_DATABASE_URL="postgres://example-not-connected",
        T100_RENDER_SENTINEL_DISCORD_WEBHOOK_URL=
            "https://discord.com/api/webhooks/123/unused-fake-placeholder",
    )
    env.update(kwargs)
    return env


class WitnessUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_witness_is_uncertified_not_healthy(self):
        db=FakeConn()
        self.assertIsNone(await RenderShadowWitness(db,scope_sha256=SCOPE).restore())

    async def test_first_write_and_sticky_latch_under_a_second_process(self):
        db=FakeConn()
        writer=RenderShadowWitness(db,scope_sha256=SCOPE)
        start=await writer.record(
            run_id=RUN1,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,
        )
        self.assertEqual(start.revision,1)
        self.assertFalse(start.kill_latched)
        killed=await writer.record(
            run_id=RUN1,cycle_index=1,state="OBSERVING_KILL_LATCHED",
            kill_latched=True,kill_reason="NATIVE_PROTECTION_LOST",
        )
        self.assertEqual(killed.revision,2)
        self.assertTrue(killed.kill_latched)
        resumed=await writer.record(
            run_id=RUN2,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,
        )
        self.assertTrue(resumed.kill_latched)
        self.assertEqual(resumed.kill_reason,"NATIVE_PROTECTION_LOST")
        self.assertEqual(resumed.state,"OBSERVING_KILL_LATCHED")
        self.assertEqual(resumed.revision,3)

    async def test_improper_scope_and_kill_reason_fail_closed(self):
        db=FakeConn()
        with self.assertRaisesRegex(ShadowWitnessFailure, "SCOPE_INVALID"):
            RenderShadowWitness(db,scope_sha256="bad")
        writer=RenderShadowWitness(db,scope_sha256=SCOPE)
        with self.assertRaisesRegex(ShadowWitnessFailure, "INPUT_INVALID"):
            await writer.record(
                run_id=RUN1,cycle_index=0,state="OBSERVING_KILL_LATCHED",
                kill_latched=True,kill_reason="SECRET_FROM_EXCHANGE",
            )
        self.assertEqual(db.calls,[])

    async def test_db_error_is_redacted_not_raw_exception(self):
        db=FakeConn(fail=True)
        writer=RenderShadowWitness(db,scope_sha256=SCOPE)
        with self.assertRaises(ShadowWitnessFailure) as caught:
            await writer.restore()
        self.assertEqual(str(caught.exception),"SHADOW_WITNESS_READ_FAILED")
        self.assertNotIn("SECRET",str(caught.exception))
        with self.assertRaisesRegex(ShadowWitnessFailure,"SHADOW_WITNESS_WRITE_FAILED"):
            await writer.record(
                run_id=RUN1,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
                kill_latched=False,kill_reason=None,
            )

    async def test_malformed_older_record_never_silent_clear(self):
        for change in [
            dict(revision=0), dict(kill_latched=True,kill_reason=None),
            dict(kill_latched=True,kill_reason="WRONG_REASONS",
                 state="OBSERVING_KILL_LATCHED"),
            dict(age_seconds=-10.0),dict(run_id="wrong"),
            dict(scope_sha256="wrong"),
        ]:
            with self.subTest(change=change):
                row=dict(
                    scope_sha256=SCOPE,revision=1,run_id=RUN1,
                    cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
                    kill_latched=False,kill_reason=None,age_seconds=0.0,
                )
                row.update(change)
                with self.assertRaisesRegex(ShadowWitnessFailure,"ROW_INVALID"):
                    parse_record(row,SCOPE)


class WitnessRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_or_live_control_never_connect(self):
        for bad in [
            {},{"T100_EXECUTION_MODE":"live"},
            {"T100_LIVE_ORDER_API_ENABLED":"true"},
            {"T100_LIVE_KILL_SWITCH":"false"},
            {"T100_RENDER_SHADOW_SCOPE_SHA256":"bad"},
        ]:
            with self.subTest(bad=bad):
                env=writer_env(**bad)
                if bad=={}:
                    env.pop("T100_RENDER_SHADOW_WITNESS_ENABLED")
                with self.assertRaises(WitnessRuntimeHalted):
                    runtime_config(env)

    async def test_healthy_one_cycle_commit_non_authorizing(self):
        db=FakeConn()
        rt=FakeRuntime()
        out=await run_witness(
            environ=writer_env(),max_cycles=1,
            runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
        )
        self.assertEqual(out.state,"OBSERVING_NON_AUTHORIZING")
        self.assertFalse(out.kill_latched)
        self.assertFalse(out.live_authorization)
        self.assertEqual(rt.cycles,1)
        self.assertTrue(rt.closed)
        self.assertTrue(db.closed)

    async def test_restart_restore_kill_before_any_exchange_cycle(self):
        db=FakeConn(existing=dict(
            scope_sha256=SCOPE, revision=12, run_id=RUN1,
            cycle_index=22,state="OBSERVING_KILL_LATCHED",
            kill_latched=True,kill_reason="NATIVE_PROTECTION_LOST",age_seconds=4.0,
        ))
        rt=FakeRuntime(run_id=RUN2)
        saved=await run_witness(
            environ=writer_env(),max_cycles=1,
            runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
        )
        self.assertEqual(rt.cycles,1)
        self.assertTrue(rt.kill.tripped)
        self.assertEqual(rt.kill.first_reason,"NATIVE_PROTECTION_LOST")
        self.assertEqual(saved.revision,13)
        self.assertTrue(saved.kill_latched)
        self.assertEqual(rt.notified,1)

    async def test_cycle_exception_durably_trips_first(self):
        db=FakeConn()
        rt=FakeRuntime(fail=True)
        with self.assertRaisesRegex(WitnessRuntimeHalted,"RENDER_SHADOW_OBSERVATION_FAILED"):
            await run_witness(
                environ=writer_env(),max_cycles=1,
                runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
            )
        self.assertTrue(db.row["kill_latched"])
        self.assertEqual(db.row["kill_reason"],"READ_FAILURE")
        self.assertTrue(rt.closed)
        self.assertTrue(db.closed)

    async def test_write_failure_halts_worker_without_false_healthy_heartbeat(self):
        db=FakeConn(fail=True)
        rt=FakeRuntime()
        with self.assertRaisesRegex(ShadowWitnessFailure,"READ_FAILED"):
            await run_witness(
                environ=writer_env(),max_cycles=1,
                runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
            )
        self.assertEqual(rt.cycles,0)
        self.assertTrue(db.closed)

    async def test_duplicate_render_worker_lease_refuses_all_observations(self):
        db=FakeConn(fail="busy")
        rt=FakeRuntime()
        with self.assertRaisesRegex(ShadowWitnessFailure,"WRITER_ALREADY_RUNNING"):
            await run_witness(
                environ=writer_env(),max_cycles=1,
                runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
            )
        self.assertEqual(rt.cycles,0)
        self.assertTrue(db.closed)

    async def test_lease_database_failure_refuses_all_observations(self):
        db=FakeConn(fail="lease")
        rt=FakeRuntime()
        with self.assertRaisesRegex(ShadowWitnessFailure,"LEASE_FAILED"):
            await run_witness(
                environ=writer_env(),max_cycles=1,
                runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
            )
        self.assertEqual(rt.cycles,0)
        self.assertTrue(db.closed)

    async def test_old_run_id_reuse_is_rejected_before_cycle(self):
        db=FakeConn(existing=dict(
            scope_sha256=SCOPE, revision=12, run_id=RUN1,
            cycle_index=22, state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,age_seconds=0.0,
        ))
        rt=FakeRuntime(run_id=RUN1)
        with self.assertRaisesRegex(WitnessRuntimeHalted,"RUN_ID_REUSE"):
            await run_witness(
                environ=writer_env(),max_cycles=1,
                runtime_factory=lambda:rt,connect=lambda dsn:_return(db),
            )
        self.assertEqual(rt.cycles,0)


async def _return(value):
    return value


class FakeNotifier:
    def __init__(self,*args,**kwargs):
        self.notices=[]
        self.closed=False
    async def send(self,notice,*,force=False):
        self.notices.append(notice)
        return True
    async def close(self):
        self.closed=True


class SentinelUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_read_failure_stale_and_killed_are_all_alerts(self):
        rows=[
            (None,"SHADOW_WITNESS_MISSING"),
            (dict(scope_sha256=SCOPE,revision=1,run_id=RUN1,cycle_index=0,
                  state="OBSERVING_NON_AUTHORIZING",kill_latched=False,
                  kill_reason=None,age_seconds=500.0),"SHADOW_WITNESS_STALE"),
            (dict(scope_sha256=SCOPE,revision=2,run_id=RUN1,cycle_index=1,
                  state="OBSERVING_KILL_LATCHED",kill_latched=True,
                  kill_reason="NATIVE_PROTECTION_LOST",age_seconds=2.0),
             "SHADOW_WITNESS_KILL_LATCHED"),
        ]
        for row,expected in rows:
            with self.subTest(expected=expected):
                result=await inspect_witness(FakeConn(existing=row),SCOPE,stale_seconds=240)
                self.assertTrue(result.alert)
                self.assertEqual(result.status,expected)
                self.assertFalse(result.live_authorization)
        failed=await inspect_witness(FakeConn(fail=True),SCOPE,stale_seconds=240)
        self.assertEqual(failed.status,"SHADOW_WITNESS_DB_READ_FAILED")

    async def test_recent_unlatched_is_nonauthorizing(self):
        db=FakeConn(existing=dict(
            scope_sha256=SCOPE,revision=5,run_id=RUN1,cycle_index=1,
            state="OBSERVING_NON_AUTHORIZING",kill_latched=False,
            kill_reason=None,age_seconds=1.0,
        ))
        result=await inspect_witness(db,SCOPE,stale_seconds=240)
        self.assertEqual(result.status,"SHADOW_WITNESS_RECENT_NONAUTHORIZING")
        self.assertFalse(result.alert)

    async def test_config_never_allows_exchange_keys_or_live_execution(self):
        for changed in (
            {"MEXC_API_KEY":"SECRET"},
            {"MEXC_API_SECRET":"SECRET"},
            {"T100_LIVE_ORDER_API_ENABLED":"true"},
            {"T100_EXECUTION_MODE":"live"},
            {"T100_RENDER_SENTINEL_ENABLED":"false"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(RenderSentinelHalted):
                    render_sentinel_config(sentinel_env(**changed))

    async def test_alarm_sends_only_status_no_db_secrets(self):
        db=FakeConn(existing=None)
        notifier=FakeNotifier()
        out=await run_sentinel(
            environ=sentinel_env(),cycles=1,
            connect=lambda dsn:_return(db),
            notifier_factory=lambda *args,**kwargs:notifier,
        )
        self.assertEqual(out.status,"SHADOW_WITNESS_MISSING")
        self.assertEqual(len(notifier.notices),1)
        self.assertEqual(notifier.notices[0].description,
                         "SHADOW_WITNESS_MISSING (nonauthorizing; operator review required)")
        self.assertTrue(notifier.closed)
        self.assertTrue(db.closed)


if __name__=="__main__":
    unittest.main()
