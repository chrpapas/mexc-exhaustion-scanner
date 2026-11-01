"""Offline account reconciliation policy tests."""
import unittest
from app.t100_live_account_reconciliation import assess_account_reconciliation

def proof(*, positions=0, stops=0, stop_count=None, limit=0, plan=0, track=0,
          complete=True, stable=True):
    if stop_count is None:
        stop_count=stops
    counts=dict(
        stopOrderCount=stop_count,
        limitOrderCount=limit,
        planOrderCount=plan,
        trackOrderCount=track,
    )
    counts["sumCount"]=stop_count+limit+plan+track
    return {
        "account_wide_completeness_verified":complete,
        "critical_inventory_equal_across_witness_window":stable,
        "after_positions":positions,
        "after_current_tpsl":stops,
        "open_order_counts":counts,
    }

class AccountReconciliationTests(unittest.TestCase):
    def test_flat_complete_account_reconciles(self):
        r=assess_account_reconciliation(proof())
        self.assertTrue(r["account_reconciled"])
        self.assertTrue(r["flat_account_reconciled"])
        self.assertFalse(r["exchange_atomicity_verified"])
        self.assertFalse(r["live_authorization"])

    def test_incomplete_or_drifting_certificate_blocks(self):
        for p in (proof(complete=False),proof(stable=False)):
            r=assess_account_reconciliation(p)
            self.assertFalse(r["account_reconciled"])

    def test_unrelated_orders_block_flat(self):
        r=assess_account_reconciliation(proof(limit=1))
        self.assertFalse(r["account_reconciled"])
        self.assertEqual(r["status"],"UNRELATED_INFLIGHT_ORDERS_PRESENT")

    def test_nonflat_requires_durable_recovery(self):
        p=proof(positions=1,stops=1)
        r=assess_account_reconciliation(p)
        self.assertFalse(r["account_reconciled"])
        self.assertEqual(r["status"],"NONFLAT_DURABLE_RECOVERY_REQUIRED")
        r=assess_account_reconciliation(
            p,durable_nonflat_recovery_verified=True)
        self.assertTrue(r["account_reconciled"])
        self.assertTrue(r["managed_nonflat_account_reconciled"])

    def test_unprotected_nonflat_blocks_even_with_recovery_flag(self):
        r=assess_account_reconciliation(
            proof(positions=1,stops=0),
            durable_nonflat_recovery_verified=True)
        self.assertFalse(r["account_reconciled"])

    def test_nonflat_zero_native_tpsl_and_limit_order_both_reported(self):
        evidence = proof(positions=1, stops=0, limit=1)
        r = assess_account_reconciliation(evidence)
        self.assertEqual(r["status"], "UNRELATED_INFLIGHT_ORDERS_PRESENT")
        self.assertEqual(r["highest_priority_blocker"],
                         "NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL")
        self.assertEqual(r["reconciliation_blockers"], [
            "NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL",
            "UNRELATED_INFLIGHT_ORDERS_PRESENT",
            "DURABLE_NONFLAT_RECOVERY_UNVERIFIED",
        ])
        self.assertTrue(r["observed_nonflat_without_current_tpsl"])
        self.assertEqual(r["observed_unrelated_limit_orders"], 1)
        self.assertFalse(r["account_reconciled"])
        self.assertFalse(r["flat_account_reconciled"])
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])
        # A manually supplied recovery boolean cannot manufacture a stop.
        manual = assess_account_reconciliation(
            evidence, durable_nonflat_recovery_verified=True,
        )
        self.assertFalse(manual["account_reconciled"])
        self.assertEqual(manual["reconciliation_blockers"], [
            "NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL",
            "UNRELATED_INFLIGHT_ORDERS_PRESENT",
        ])

    def test_complete_flat_zero_orders_has_no_spurious_blockers(self):
        result = assess_account_reconciliation(proof())
        self.assertEqual(result["reconciliation_blockers"], [])
        self.assertFalse(result["observed_nonflat_without_current_tpsl"])
        self.assertEqual(result["observed_unrelated_limit_orders"], 0)

    def test_incomplete_or_drifting_data_cannot_claim_zero_native_tpsl(self):
        for payload in (
            proof(positions=1, stops=0, limit=1, complete=False),
            proof(positions=1, stops=0, limit=1, stable=False),
            proof(positions=1, stops=0, stop_count=1),
        ):
            r = assess_account_reconciliation(payload)
            self.assertFalse(r["observed_nonflat_without_current_tpsl"])
            self.assertEqual(r["reconciliation_blockers"], [])
            self.assertFalse(r["account_reconciled"])

    def test_tpsl_count_drift_blocks(self):
        r=assess_account_reconciliation(
            proof(positions=1,stops=1,stop_count=2),
            durable_nonflat_recovery_verified=True)
        self.assertFalse(r["account_reconciled"])
        self.assertEqual(r["status"],"ACCOUNT_TPSL_COUNT_DRIFT")

if __name__=="__main__":
    unittest.main()
