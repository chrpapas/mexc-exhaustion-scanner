"""Static contract for the restricted T100 live runtime grants."""
from pathlib import Path
import unittest


PATH = Path("sql/t100_live_runtime_privileges_template.sql")


class RuntimePrivilegeTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = PATH.read_text(encoding="utf-8").lower()

    def test_template_cannot_be_mistaken_for_concrete_role(self):
        self.assertIn("__t100_live_runtime_role__", self.sql)
        self.assertIn("do not run unchanged", self.sql)

    def test_mutable_tables_get_update_but_never_delete_or_truncate(self):
        self.assertIn("grant select, insert, update on table", self.sql)
        self.assertNotIn("grant delete", self.sql)
        self.assertNotIn("grant truncate", self.sql)

    def test_append_only_tables_are_select_insert_only(self):
        marker = "-- immutable evidence: append + read only."
        start = self.sql.index(marker)
        end = self.sql.index("-- research/v40 full-strategy", start)
        block = self.sql[start:end]
        self.assertIn("grant select, insert on table", block)
        self.assertNotIn("update", block)
        self.assertNotIn("delete", block)
        self.assertNotIn("truncate", block)

    def test_full_v40_state_only_grants_column_level_updates(self):
        self.assertIn("grant update (status) on table", self.sql)
        self.assertIn("t100_live_vol30_parents", self.sql)
        self.assertIn(
            "grant update (state, filled_notional_usdt, "
            "exchange_order_id, updated_at)",
            self.sql,
        )
        self.assertIn("t100_live_vol30_children", self.sql)
        self.assertIn(
            "grant update (holder_id, fencing_token, "
            "lease_expires_at, updated_at)",
            self.sql,
        )
        self.assertIn("t100_live_executor_leases", self.sql)
        self.assertNotIn(
            "grant select, insert, update on table\n"
            "    t100_live_vol30_parents", self.sql,
        )

    def test_sequences_get_usage_select_without_update(self):
        marker = "grant usage, select on sequence"
        self.assertIn(marker, self.sql)
        tail = self.sql[self.sql.index(marker):]
        self.assertNotIn("grant update on sequence", tail)


if __name__ == "__main__":
    unittest.main()
