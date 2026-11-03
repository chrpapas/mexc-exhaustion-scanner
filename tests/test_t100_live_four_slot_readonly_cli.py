"""Manual signed GET + restricted-read CLI safe entrypoint regressions."""
from __future__ import annotations

import unittest
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from app.t100_live_four_slot_readonly_cli import (
    _main, run_readonly_four_slot_audit,
)
from tests.test_t100_live_four_slot_mexc_readonly import (
    MockMexcGetOnly, prepare_rows,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Ledger,
)


PAPER_KILLED = {
    "T100_EXECUTION_MODE": "paper",
    "T100_LIVE_ORDER_API_ENABLED": "false",
    "T100_LIVE_KILL_SWITCH": "true",
}


class FourSlotOperatorCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_three_owned_slots_are_observation_not_live_authorization(self):
        snapshot, orders, positions, stops = prepare_rows(3)
        cli_result = await run_readonly_four_slot_audit(
            ledger=Ledger(snapshot),
            client=MockMexcGetOnly(orders, positions, stops),
            env=PAPER_KILLED,
        )
        self.assertEqual(
            cli_result["status"],
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(cli_result["owned_four_slot_positions_observed"], 3)
        self.assertFalse(cli_result["live_authorization"])
        self.assertFalse(cli_result["mexc_orders_sent"])
        self.assertFalse(cli_result["database_writes_sent"])
        self.assertFalse(cli_result["cross_margin_liquidation_certified"])

    async def test_manual_position_blocks_without_owned_checkpoint(self):
        snap, _, _, _ = prepare_rows(0)
        _, orders, positions, stops = prepare_rows(1)
        result = await run_readonly_four_slot_audit(
            ledger=Ledger(snap),
            client=MockMexcGetOnly(orders, positions, stops),
            env=PAPER_KILLED,
        )
        self.assertEqual(result["status"], "UNOWNED_OR_MISSING_CHECKPOINTS")
        self.assertIsNone(result["owned_four_slot_positions_observed"])
        self.assertFalse(result["entry_allowed"])

    async def test_extra_current_tpsl_yields_redacted_matching_counts(self):
        import json
        snap, orders, positions, stops = prepare_rows(1)
        extra = {
            **stops[0], "id": "another-private-stop",
            "stopLossPrice": "0", "takeProfitPrice": "80",
        }
        client = MockMexcGetOnly(orders, positions, [*stops, extra])
        output = await run_readonly_four_slot_audit(
            ledger=Ledger(snap), client=client, env=PAPER_KILLED,
        )
        self.assertEqual(output["status"], "POSITION_STOP_COUNT_MISMATCH")
        d = output["stop_count_diagnostic"]
        self.assertEqual(d["position_count"], 1)
        self.assertEqual(d["current_tpsl_row_count"], 2)
        self.assertEqual(d["positions_with_multiple_current_tpsl_rows"], 1)
        self.assertEqual(d["current_tpsl_rows_without_positive_stop_loss"], 1)
        self.assertFalse(d["native_stop_coverage_certified"])
        self.assertFalse(output["live_authorization"])
        self.assertFalse(output["entry_allowed"])
        summary = json.dumps(output)
        for item in ("another-private-stop", "SIREN_USDT", "80"):
            self.assertNotIn(item, summary)

    async def test_open_position_without_current_tpsl_reports_gap_not_authorization(self):
        snap, orders, positions, stops = prepare_rows(1)
        output = await run_readonly_four_slot_audit(
            ledger=Ledger(snap),
            client=MockMexcGetOnly(orders, positions, []),
            env=PAPER_KILLED,
        )
        self.assertEqual(output["status"], "POSITION_STOP_COUNT_MISMATCH")
        d = output["stop_count_diagnostic"]
        self.assertEqual(d["position_count"], 1)
        self.assertEqual(d["current_tpsl_row_count"], 0)
        self.assertEqual(d["positions_without_any_current_tpsl"], 1)
        self.assertEqual(d["positions_without_positive_stop_loss_row"], 1)
        self.assertFalse(output["live_authorization"])

    async def test_current_tpsl_without_open_position_is_unmatched(self):
        snap, _, _, _ = prepare_rows(0)
        _, _, _, stops = prepare_rows(1)
        output = await run_readonly_four_slot_audit(
            ledger=Ledger(snap),
            client=MockMexcGetOnly([], [], stops),
            env=PAPER_KILLED,
        )
        self.assertEqual(output["status"], "POSITION_STOP_COUNT_MISMATCH")
        d = output["stop_count_diagnostic"]
        self.assertEqual(d["position_count"], 0)
        self.assertEqual(d["current_tpsl_row_count"], 1)
        self.assertEqual(d["current_tpsl_rows_with_unmatched_position"], 1)
        self.assertFalse(output["entry_allowed"])

    async def test_disabled_kill_refuses_before_any_database_or_get(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        for env in (
            {},
            {**PAPER_KILLED, "T100_EXECUTION_MODE": "live"},
            {**PAPER_KILLED, "T100_LIVE_ORDER_API_ENABLED": "true"},
            {**PAPER_KILLED, "T100_LIVE_KILL_SWITCH": "false"},
        ):
            with self.subTest(env=env):
                result = await run_readonly_four_slot_audit(
                    ledger=Ledger(snap), client=client, env=env,
                )
                self.assertEqual(
                    result["status"], "FOUR_SLOT_READONLY_PAPER_KILL_REQUIRED",
                )
        self.assertEqual(client.calls, [])

    async def test_restart_preflight_cli_opt_in_routes_through_restricted_sql(self):
        """No actual credential, network GET or POST is used in this test."""
        from app.t100_live_four_slot_restart_triage import (
            PostgresFourSlotRestartTriage,
        )
        from tests.test_t100_live_four_slot_restart_triage import (
            FakePool, sample,
        )
        fake_pool = FakePool([sample()])
        fake_pool.close = AsyncMock()
        client = MockMexcGetOnly([], [], [])
        client.close = AsyncMock()
        fake_asyncpg = ModuleType("asyncpg")
        fake_asyncpg.create_pool = AsyncMock(return_value=fake_pool)
        fake_mexc = ModuleType("app.mexc_trade")
        fake_mexc.MexcTradeClient = MagicMock(return_value=client)

        env = {
            **PAPER_KILLED,
            "MEXC_API_KEY": "fake-ci-key",
            "MEXC_API_SECRET": "fake-ci-secret",
        }
        with patch.dict(
            "sys.modules",
            {"asyncpg": fake_asyncpg, "app.mexc_trade": fake_mexc},
        ), patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=["ci-test-password", "postgres://fake-ci-host"],
        ):
            result = await _main(env, restart_preflight=True)
        self.assertEqual(
            result["status"],
            "RESTART_PENDING_ATTEMPT_EXACT_ORIGIN_RECONCILE_ONLY",
        )
        self.assertEqual(result["pending_v40_attempts"], 1)
        self.assertEqual(client.calls, [])
        self.assertGreater(len(fake_pool.conn.calls), 0)
        fake_asyncpg.create_pool.assert_awaited_once()
        # A pending SQL attempt now blocks BEFORE importing/creating
        # the general exchange client, even in the fake integration.
        fake_mexc.MexcTradeClient.assert_not_called()
        client.close.assert_not_awaited()
        fake_pool.close.assert_awaited_once()
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertNotIn("ci-test-password", str(result))
        self.assertNotIn("fake-ci-secret", str(result))

    async def test_restart_preflight_rechecks_db_after_client_construction(self):
        """The second SQL witness must detect new SUBMITTING attempts."""
        from tests.test_t100_live_four_slot_restart_triage import (
            FakePool, sample,
        )
        fake_pool = FakePool([])
        fake_pool.conn.fetch = AsyncMock(side_effect=[[], [sample()]])
        fake_pool.close = AsyncMock()
        client = MockMexcGetOnly([], [], [])
        client.close = AsyncMock()
        fake_asyncpg = ModuleType("asyncpg")
        fake_asyncpg.create_pool = AsyncMock(return_value=fake_pool)
        fake_mexc = ModuleType("app.mexc_trade")
        fake_mexc.MexcTradeClient = MagicMock(return_value=client)

        env = {
            **PAPER_KILLED,
            "MEXC_API_KEY": "fixture-key",
            "MEXC_API_SECRET": "fixture-secret",
        }
        with patch.dict(
            "sys.modules",
            {"asyncpg": fake_asyncpg, "app.mexc_trade": fake_mexc},
        ), patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=["fixture-password", "postgres://test-connection"],
        ):
            result = await _main(env, restart_preflight=True)
        self.assertEqual(
            result["status"],
            "RESTART_PENDING_ATTEMPT_EXACT_ORIGIN_RECONCILE_ONLY",
        )
        self.assertEqual(fake_pool.conn.fetch.await_count, 2)
        fake_mexc.MexcTradeClient.assert_called_once()
        self.assertEqual(client.calls, [])
        client.close.assert_awaited_once()
        fake_pool.close.assert_awaited_once()
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["mexc_orders_sent"])

    async def test_pending_origin_audit_cli_double_get_only_never_posts(self):
        """Explicit opt-in may query unresolved origins, never open a trade."""
        from tests.test_t100_live_four_slot_restart_triage import (
            FakePool, sample,
        )

        pool = FakePool([sample()])
        pool.close = AsyncMock()
        order = {
            "symbol": "EXAMPLE_USDT", "externalOid": "opaque-order-origin",
            "orderId": "901", "side": 3, "state": 3, "dealVol": "1",
        }
        client = MockMexcGetOnly([order], [], [])
        client.close = AsyncMock()
        asyncpg_module = ModuleType("asyncpg")
        asyncpg_module.create_pool = AsyncMock(return_value=pool)
        mexc_module = ModuleType("app.mexc_trade")
        mexc_module.MexcTradeClient = MagicMock(return_value=client)
        env = {
            **PAPER_KILLED, "MEXC_API_KEY": "fictional-test-key",
            "MEXC_API_SECRET": "fictional-test-secret",
        }
        with patch.dict(
            "sys.modules",
            {"asyncpg": asyncpg_module, "app.mexc_trade": mexc_module},
        ), patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=["fictional-password", "postgres://dummy-host"],
        ):
            result = await _main(env, pending_origin_audit=True)
        self.assertEqual(
            result["status"],
            "PENDING_ORIGIN_ORDERS_SEEN_UNPROTECTED_NONAUTHORIZING",
        )
        self.assertEqual(result["origins_checked"], 1)
        self.assertEqual(result["origin_orders_seen"], 1)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            all("/api/v1/private/order/external/" in path
                for path, _ in client.calls)
        )
        mexc_module.MexcTradeClient.assert_called_once()
        client.close.assert_awaited_once()
        pool.close.assert_awaited_once()
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["retry_allowed"])
        self.assertFalse(result["database_writes_sent"])
        self.assertNotIn(order["externalOid"], str(result))
        self.assertNotIn("fictional-test-secret", str(result))

    async def test_pending_origin_audit_sql_drift_aborts_before_venue_get(self):
        from tests.test_t100_live_four_slot_restart_triage import (
            FakePool, sample,
        )
        pool = FakePool([sample()])
        pool.conn.fetch = AsyncMock(side_effect=[[sample()], []])
        pool.close = AsyncMock()
        client = MockMexcGetOnly([], [], [])
        client.close = AsyncMock()
        asyncpg_module = ModuleType("asyncpg")
        asyncpg_module.create_pool = AsyncMock(return_value=pool)
        mexc_module = ModuleType("app.mexc_trade")
        mexc_module.MexcTradeClient = MagicMock(return_value=client)
        env = {
            **PAPER_KILLED, "MEXC_API_KEY": "fixture-key",
            "MEXC_API_SECRET": "fixture-secret",
        }
        with patch.dict(
            "sys.modules",
            {"asyncpg": asyncpg_module, "app.mexc_trade": mexc_module},
        ), patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=["fixture-password", "postgres://dummy-host"],
        ):
            result = await _main(env, pending_origin_audit=True)
        self.assertEqual(
            result["status"], "PENDING_ORIGIN_SQL_CHANGED_NO_RETRY",
        )
        self.assertEqual(pool.conn.fetch.await_count, 2)
        self.assertEqual(client.calls, [])
        self.assertFalse(result["mexc_orders_sent"])
        client.close.assert_awaited_once()

    async def test_pending_origin_audit_requires_pending_before_client_creation(self):
        from tests.test_t100_live_four_slot_restart_triage import FakePool
        pool = FakePool([])
        pool.close = AsyncMock()
        asyncpg_module = ModuleType("asyncpg")
        asyncpg_module.create_pool = AsyncMock(return_value=pool)
        mexc_module = ModuleType("app.mexc_trade")
        mexc_module.MexcTradeClient = MagicMock(
            side_effect=AssertionError("no client if no pending order")
        )
        env = {
            **PAPER_KILLED, "MEXC_API_KEY": "fixture-key",
            "MEXC_API_SECRET": "fixture-secret",
        }
        with patch.dict(
            "sys.modules",
            {"asyncpg": asyncpg_module, "app.mexc_trade": mexc_module},
        ), patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=["fixture-password", "postgres://dummy-host"],
        ):
            result = await _main(env, pending_origin_audit=True)
        self.assertEqual(result["status"], "PENDING_ORIGIN_SQL_TRIAGE_REQUIRED")
        mexc_module.MexcTradeClient.assert_not_called()
        pool.close.assert_awaited_once()

    async def test_pending_origin_and_restart_flags_are_mutually_exclusive(self):
        with patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=AssertionError("no credential prompts allowed"),
        ):
            r = await _main(
                PAPER_KILLED, pending_origin_audit=True,
                restart_preflight=True,
            )
        self.assertEqual(r["status"], "FOUR_SLOT_READONLY_MODE_CONFLICT")
        self.assertFalse(r["live_authorization"])

    async def test_restart_preflight_refuses_before_any_credential_prompt(self):
        with patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=AssertionError("must not ask password"),
        ):
            result = await _main({}, restart_preflight=True)
        self.assertEqual(
            result["status"], "FOUR_SLOT_READONLY_PAPER_KILL_REQUIRED"
        )

    async def test_invalid_controls_block_before_hidden_prompt(self):
        with patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=AssertionError("must not prompt"),
        ):
            result = await _main({})
        self.assertEqual(result["status"], "FOUR_SLOT_READONLY_PAPER_KILL_REQUIRED")

    async def test_missing_credentials_block_before_hidden_db_prompt(self):
        with patch(
            "app.t100_live_four_slot_readonly_cli.getpass.getpass",
            side_effect=AssertionError("must not prompt"),
        ):
            result = await _main(PAPER_KILLED)
        self.assertEqual(result["status"], "FOUR_SLOT_READONLY_CREDENTIALS_MISSING")

    async def test_get_outage_reports_no_account_information(self):
        snap, orders, positions, stops = prepare_rows(1)
        client = MockMexcGetOnly(orders, positions, stops)
        client.fail_path = "/api/v1/private/account/assets"
        result = await run_readonly_four_slot_audit(
            ledger=Ledger(snap), client=client, env=PAPER_KILLED,
        )
        self.assertEqual(result["status"], "INVENTORY_COMPLETENESS_UNVERIFIED")
        self.assertIsNone(result["owned_four_slot_positions_observed"])
        self.assertFalse(result["entry_allowed"])

    def test_no_post_or_db_write_in_invocation_logic(self):
        import inspect
        import app.t100_live_four_slot_readonly_cli as module
        source = inspect.getsource(module)
        for forbidden in (
            "._private_post(", "submit_short_with_stop(",
            "PostgresProtectedV40Commit(", "LiveMutationPermit",
            "conn.execute(", "pool.execute(", "trigger_deploy(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
