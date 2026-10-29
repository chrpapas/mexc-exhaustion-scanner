"""Static safety checks for the create-only live durable migration."""
from pathlib import Path
import re
import unittest


MIGRATION = Path("migrations/029_t100_live_durable_state.sql")


class DurableMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.lower = cls.sql.lower()

    def test_migration_is_create_only_for_live_namespace(self):
        self.assertNotRegex(
            self.lower,
            r"\b(drop|truncate)\b",
        )
        self.assertNotRegex(
            self.lower,
            r"\balter\s+table\b",
        )
        for paper_table in (
            "t100_positions",
            "t100_equity_snapshots",
            "trader_positions",
        ):
            self.assertNotIn(
                f"create table if not exists {paper_table}",
                self.lower,
            )
            self.assertNotRegex(
                self.lower,
                rf"\b(update|delete\s+from|insert\s+into)\s+{paper_table}\b",
            )

    def test_required_live_tables_exist(self):
        expected = (
            "t100_live_position_checkpoints",
            "t100_live_position_checkpoint_revisions",
            "t100_live_stop_amendments",
            "t100_live_order_fill_sets",
            "t100_live_order_fills",
            "t100_live_position_funding_sets",
            "t100_live_funding_events",
            "t100_live_final_position_accounting",
        )
        for table in expected:
            with self.subTest(table=table):
                self.assertIn(
                    f"create table if not exists {table}",
                    self.lower,
                )

    def test_stop_amendments_preserve_unknown_outcome_state(self):
        self.assertIn(
            "('prepared','submitting','unknown','verified')",
            re.sub(r"\s+", "", self.lower),
        )
        self.assertIn("verified_stop_order_id", self.lower)

    def test_checkpoint_revision_history_has_unique_version(self):
        compact = re.sub(r"\s+", " ", self.lower)
        self.assertIn(
            "unique(position_id, checkpoint_version)",
            compact,
        )
        self.assertIn("'stop_verified'", self.lower)
        self.assertIn("'close_verified'", self.lower)

    def test_verified_accounting_requires_equal_open_and_close_volume(self):
        compact = re.sub(r"\s+", " ", self.lower)
        self.assertIn(
            "check (opened_contracts = closed_contracts)",
            compact,
        )

    def test_append_only_runtime_permission_gate_is_explicit(self):
        self.assertIn(
            "runtime-role update/delete revocation",
            self.lower,
        )


if __name__ == "__main__":
    unittest.main()
