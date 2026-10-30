"""Manual signed GET + restricted-read CLI safe entrypoint regressions."""
from __future__ import annotations

import unittest
from unittest.mock import patch

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
