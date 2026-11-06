"""Offline fail-closed guardian worker tests.

The worker is GET + SELECT only. These tests inject a fake one-cycle
observer and no real MEXC, PostgreSQL, Render or Discord resources.
"""
from __future__ import annotations

import json
import unittest

from app.t100_live_guardian_readonly_once import (
    GuardianOnceHalted, ReadOnlyCycleSummary,
)
from app.t100_live_guardian_readonly_worker import (
    GuardianWorkerConfig, _classify, run_worker,
)
from tests.test_t100_live_guardian_readonly_once import env as once_env


def worker_env(**kwargs):
    return dict(once_env(),T100_GUARDIAN_WORKER_ENABLED="true",**kwargs)


def report(**kwargs):
    row=dict(
        status="OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED",
        managed_position_observed=True,
        native_market_stop_verified=True,
        foreign_position_count=1,
        alert=False,
    )
    row.update(kwargs)
    return ReadOnlyCycleSummary(**row)


class ConfigTests(unittest.TestCase):
    def test_separate_opt_in_and_paper_mode_required(self):
        valid=GuardianWorkerConfig.from_env(worker_env())
        self.assertEqual(valid.interval_seconds,90)
        self.assertEqual(valid.maximum_failures,3)
        for change in (
            dict(T100_GUARDIAN_WORKER_ENABLED="false"),
            dict(T100_GUARDIAN_OBSERVER_ENABLED="false"),
            dict(T100_EXECUTION_MODE="live"),
            dict(T100_LIVE_ORDER_API_ENABLED="true"),
            dict(T100_LIVE_KILL_SWITCH="false"),
            dict(T100_GUARDIAN_ACCOUNT_KEY_SHA256="f"*64),
            dict(T100_GUARDIAN_POSITION_ID="777x"),
            dict(T100_GUARDIAN_READONLY_DB_DSN=""),
            dict(T100_GUARDIAN_DB_CA_FILE=""),
            dict(T100_GUARDIAN_MEXC_API_KEY=""),
        ):
            with self.subTest(changed=sorted(change)):
                with self.assertRaises(Exception):
                    GuardianWorkerConfig.from_env(worker_env(**change))

    def test_invalid_cycle_interval_and_failure_limit_denied(self):
        for changes in (
            dict(T100_GUARDIAN_INTERVAL_SECONDS="10"),
            dict(T100_GUARDIAN_INTERVAL_SECONDS="3601"),
            dict(T100_GUARDIAN_INTERVAL_SECONDS="nan"),
            dict(T100_GUARDIAN_MAX_FAILURES="0"),
            dict(T100_GUARDIAN_MAX_FAILURES="6"),
            dict(T100_GUARDIAN_MAX_FAILURES="not-an-integer"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(GuardianOnceHalted):
                    GuardianWorkerConfig.from_env(worker_env(**changes))


class ClassificationTests(unittest.TestCase):
    def test_positive_stop_protection_not_live_authorization(self):
        x=_classify(report(),1,0)
        self.assertEqual(x.status,"OBSERVER_CYCLE_OK")
        self.assertFalse(x.alert)
        self.assertTrue(x.managed_position_observed)
        self.assertTrue(x.foreign_positions_seen)
        self.assertFalse(x.entry_allowed)
        self.assertFalse(x.emergency_close_allowed)
        self.assertFalse(x.live_authorization)
        self.assertEqual(x.mexc_orders_sent,0)

    def test_no_checkpoint_is_not_false_green(self):
        x=_classify(report(
            status="NO_MANAGED_CHECKPOINT_NO_EXCHANGE_ACTION",
            managed_position_observed=False,
            native_market_stop_verified=False,
            alert=False),2,1)
        self.assertTrue(x.alert)
        self.assertEqual(x.status,"OBSERVER_UNENROLLED")
        self.assertFalse(x.managed_position_observed)

    def test_owned_closure_not_flat_account_or_new_entry(self):
        x=_classify(report(
            status="OWNED_POSITION_CLOSED_IN_EXCHANGE_HISTORY",
            managed_position_observed=False,
            native_market_stop_verified=False,
            alert=False),1,0)
        self.assertFalse(x.alert)
        self.assertEqual(x.status,"OBSERVER_CYCLE_INACTIVE")
        self.assertFalse(x.entry_allowed)
        self.assertFalse(x.emergency_close_allowed)

    def test_false_protection_boolean_alerts(self):
        for changed in (
            dict(managed_position_observed=False),
            dict(native_market_stop_verified=False),
            dict(alert=True),
            dict(status="OWNED_NATIVE_STOP_MISSING"),
            dict(status="CLOSE_ACK_UNVERIFIED_RECONCILE_ONLY"),
        ):
            with self.subTest(changed=changed):
                x=_classify(report(**changed),1,1)
                self.assertTrue(x.alert)
                self.assertEqual(x.status,"OBSERVER_SAFETY_ALERT")

    def test_foreign_positions_count_not_logged(self):
        x=_classify(report(foreign_position_count=98765),1,0)
        serialized=json.dumps(x.__dict__)
        self.assertNotIn("98765",serialized)
        self.assertTrue(x.foreign_positions_seen)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_one_cycle_records_no_orders(self):
        rows=[]
        async def observe(_env):
            return report()
        async def pause(_seconds):
            raise AssertionError("once should not sleep")
        code=await run_worker(worker_env(),observe=observe,
                              pause=pause,emit=rows.append,max_cycles=1)
        self.assertEqual(code,0)
        self.assertEqual(len(rows),1)
        x=json.loads(rows[0])
        self.assertFalse(x["live_authorization"])
        self.assertFalse(x["emergency_close_allowed"])
        self.assertEqual(x["mexc_orders_sent"],0)
        self.assertEqual(x["database_mutations"],0)

    async def test_consecutive_unknown_reads_exit_fail_closed(self):
        rows=[]
        calls=[]
        async def observe(_env):
            calls.append("get-only")
            return report(status="OWNED_STOP_MISSING_TRIGGER_STATUS_UNCERTAIN",
                          native_market_stop_verified=False,alert=True)
        async def pause(sec):
            self.assertEqual(sec,90)
        code=await run_worker(worker_env(),observe=observe,
                              pause=pause,emit=rows.append,max_cycles=8)
        self.assertEqual(code,2)
        self.assertEqual(len(calls),3)
        self.assertEqual(len(rows),4)
        final=json.loads(rows[-1])
        self.assertEqual(final["status"],"OBSERVER_FAIL_CLOSED_FAILURE_LIMIT")
        self.assertFalse(final["entry_allowed"])
        self.assertFalse(final["emergency_close_allowed"])
        self.assertEqual(final["mexc_orders_sent"],0)

    async def test_failed_cycle_then_healthy_resets_counter(self):
        values=[
            report(status="UNKNOWN",native_market_stop_verified=False,alert=True),
            report(),
            report(status="UNKNOWN",native_market_stop_verified=False,alert=True),
        ]
        rows=[]
        async def observe(_env):
            return values.pop(0)
        async def pause(_secs):
            return None
        result=await run_worker(worker_env(),observe=observe,pause=pause,
                                emit=rows.append,max_cycles=3)
        self.assertEqual(result,1)
        self.assertEqual([json.loads(x)["consecutive_failures"] for x in rows],
                         [1,0,1])

    async def test_secret_in_exception_is_never_logged(self):
        secret="MEXC_FAKE_SECRET_SENSITIVE"
        rows=[]
        async def observe(_env):
            raise RuntimeError(secret)
        async def pause(_secs):
            pass
        result=await run_worker(worker_env(T100_GUARDIAN_MAX_FAILURES="1"),
                                observe=observe,pause=pause,
                                emit=rows.append,max_cycles=2)
        self.assertEqual(result,2)
        self.assertEqual(len(rows),2)
        self.assertNotIn(secret,"".join(rows))
        self.assertEqual(json.loads(rows[0])["status"],
                         "OBSERVER_SIGNED_GET_OR_DB_UNAVAILABLE")

    async def test_no_secret_or_db_connection_if_config_missing(self):
        called=[]
        async def observe(_env):
            called.append(True)
            return report()
        with self.assertRaises(GuardianOnceHalted):
            await run_worker(worker_env(T100_GUARDIAN_WORKER_ENABLED="false"),
                             observe=observe,max_cycles=1)
        self.assertEqual(called,[])

    async def test_unknown_report_cannot_be_greened(self):
        rows=[]
        async def observe(_env):
            return {"status":"OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED"}
        code=await run_worker(worker_env(),observe=observe,
                              emit=rows.append,max_cycles=1)
        self.assertEqual(code,1)
        self.assertEqual(json.loads(rows[0])["status"],
                         "OBSERVER_UNVERIFIED_REPORT")

    async def test_closed_observer_healthy_but_no_live_trading(self):
        rows=[]
        async def observe(_env):
            return report(status="OWNED_POSITION_CLOSED_IN_EXCHANGE_HISTORY",
                          managed_position_observed=False,
                          native_market_stop_verified=False,alert=False)
        code=await run_worker(worker_env(),observe=observe,
                              emit=rows.append,max_cycles=1)
        self.assertEqual(code,0)
        self.assertEqual(json.loads(rows[0])["status"],
                         "OBSERVER_CYCLE_INACTIVE")
        self.assertFalse(json.loads(rows[0])["live_authorization"])


if __name__ == "__main__":
    unittest.main()
