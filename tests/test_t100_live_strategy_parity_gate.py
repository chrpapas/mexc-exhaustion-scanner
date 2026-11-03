"""Offline machine-readable strategy parity gate tests."""
import unittest

from app.t100_live_strategy_parity_gate import (
    FROZEN_V40_ARCHIVE_SHA256,
    FULL_REPLAY_REQUIRED_TRUE,
    assess_strategy_parity_report,
)


def entry_report():
    return {
        "status": "V40_ENTRY_FILTER_AND_REFERENCE_CAPACITY_PARITY_PASS",
        "archive_sha256": FROZEN_V40_ARCHIVE_SHA256,
        "candidates": 146,
        "prior_candles": 7154,
        "reference_entry_notionals_exact": 648,
        "discrepancies": 0,
        "code_revision": "abc123",
    }


def full_replay():
    result = {field: True for field in FULL_REPLAY_REQUIRED_TRUE}
    result.update({
        "entry_decision_mismatches": 0,
        "exit_decision_mismatches": 0,
        "funding_mismatches": 0,
        "position_state_mismatches": 0,
        "contract_spec_mismatches": 0,
        "frozen_anchor": {
            "stage_rows": 27776,
            "episodes": 21838,
            "multi_stage_ge2": 4314,
            "multi_stage_ge3": 1172,
            "candidate_count": 146,
        },
        "reviewed_code_revision": "abc123",
    })
    return result


class StrategyParityGateTests(unittest.TestCase):
    def test_existing_entry_audit_is_not_full_strategy_parity(self):
        r = assess_strategy_parity_report(entry_report())
        self.assertTrue(r["historical_entry_parity_verified"])
        self.assertFalse(r["strategy_parity_verified"])
        self.assertFalse(r["live_execution_equivalence_verified"])
        self.assertEqual(
            r["status"], "ENTRY_PARITY_ONLY_FULL_REPLAY_MISSING"
        )

    def test_wrong_archive_or_counts_blocks_entry_parity(self):
        cases = (
            ("archive_sha256", "bad"),
            ("candidates", 145),
            ("prior_candles", 7153),
            ("reference_entry_notionals_exact", 647),
            ("discrepancies", 1),
        )
        for field, value in cases:
            report = entry_report()
            report[field] = value
            with self.subTest(field=field):
                r = assess_strategy_parity_report(report)
                self.assertFalse(r["historical_entry_parity_verified"])
                self.assertFalse(r["strategy_parity_verified"])

    def test_complete_full_replay_can_certify_historical_strategy_parity(self):
        report = entry_report()
        report["full_replay"] = full_replay()
        r = assess_strategy_parity_report(report)
        self.assertTrue(r["historical_entry_parity_verified"])
        self.assertTrue(r["full_chronological_replay_verified"])
        self.assertTrue(r["strategy_parity_verified"])
        self.assertFalse(r["live_execution_equivalence_verified"])
        self.assertFalse(r["live_authorization"])

    def test_any_full_replay_claim_or_mismatch_blocks(self):
        for field in FULL_REPLAY_REQUIRED_TRUE:
            report = entry_report()
            replay = full_replay()
            replay[field] = False
            report["full_replay"] = replay
            with self.subTest(field=field):
                self.assertFalse(
                    assess_strategy_parity_report(report)[
                        "strategy_parity_verified"
                    ]
                )

        report = entry_report()
        replay = full_replay()
        replay["exit_decision_mismatches"] = 1
        report["full_replay"] = replay
        self.assertFalse(
            assess_strategy_parity_report(report)[
                "strategy_parity_verified"
            ]
        )

    def test_reviewed_revision_must_match_report_revision(self):
        report = entry_report()
        replay = full_replay()
        replay["reviewed_code_revision"] = "different"
        report["full_replay"] = replay
        self.assertFalse(
            assess_strategy_parity_report(report)[
                "strategy_parity_verified"
            ]
        )

    def test_live_execution_equivalence_is_separate(self):
        report = entry_report()
        report["full_replay"] = full_replay()
        report["live_execution_equivalence_verified"] = True
        r = assess_strategy_parity_report(report)
        self.assertTrue(r["strategy_parity_verified"])
        self.assertTrue(r["live_execution_equivalence_verified"])


if __name__ == "__main__":
    unittest.main()
