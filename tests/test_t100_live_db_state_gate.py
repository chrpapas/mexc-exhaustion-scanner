"""Offline unresolved live-state gate tests."""
import unittest

from app.t100_live_db_state_gate import assess_unresolved_live_state


class Pool:
    def __init__(self, **counts):
        self.counts = {
            "prepared_order_intents": 0,
            "submitting_order_intents": 0,
            "unknown_order_intents": 0,
            "unresolved_stop_amendments": 0,
            "review_checkpoints": 0,
            "closing_checkpoints": 0,
        }
        self.counts.update(counts)
        self.fail = False

    async def fetchrow(self, sql):
        if self.fail:
            raise ConnectionError("db unavailable")
        return dict(self.counts)


class LiveStateGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_state_passes(self):
        r = await assess_unresolved_live_state(Pool())
        self.assertTrue(r["unresolved_mutations_absent"])
        self.assertFalse(r["live_authorization"])
        self.assertEqual(
            r["status"],
            "NO_UNRESOLVED_LIVE_MUTATIONS_OR_INTERVENTION_STATES",
        )

    async def test_each_unresolved_state_blocks(self):
        fields = (
            "prepared_order_intents",
            "submitting_order_intents",
            "unknown_order_intents",
            "unresolved_stop_amendments",
            "review_checkpoints",
            "closing_checkpoints",
        )
        for field in fields:
            with self.subTest(field=field):
                r = await assess_unresolved_live_state(
                    Pool(**{field: 1})
                )
                self.assertFalse(r["unresolved_mutations_absent"])
                self.assertEqual(
                    r["status"],
                    "UNRESOLVED_LIVE_MUTATION_OR_INTERVENTION_STATE",
                )

    async def test_invalid_count_blocks(self):
        for bad in (-1, True, None, "0"):
            with self.subTest(bad=bad):
                r = await assess_unresolved_live_state(
                    Pool(unknown_order_intents=bad)
                )
                self.assertFalse(r["unresolved_mutations_absent"])
                self.assertEqual(
                    r["status"], "LIVE_STATE_COUNT_INVALID"
                )

    async def test_database_failure_blocks(self):
        p = Pool()
        p.fail = True
        r = await assess_unresolved_live_state(p)
        self.assertFalse(r["unresolved_mutations_absent"])
        self.assertEqual(r["status"], "LIVE_STATE_READ_FAILED")


if __name__ == "__main__":
    unittest.main()
