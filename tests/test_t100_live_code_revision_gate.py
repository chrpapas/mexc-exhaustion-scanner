"""Offline exact reviewed-code revision pin tests."""
import unittest

from app.t100_live_code_revision_gate import assess_code_revision_pin


A = "a" * 40
B = "b" * 40


class CodeRevisionGateTests(unittest.TestCase):
    def test_exact_green_commit_with_auto_deploy_off_passes(self):
        r = assess_code_revision_pin(
            reviewed_commit=A,
            runtime_commit=A,
            ci_green_for_reviewed_commit=True,
            auto_deploy_disabled=True,
        )
        self.assertTrue(r["code_revision_verified"])
        self.assertFalse(r["live_authorization"])

    def test_mismatch_ci_or_auto_deploy_blocks(self):
        cases = (
            dict(
                reviewed_commit=A,
                runtime_commit=B,
                ci_green_for_reviewed_commit=True,
                auto_deploy_disabled=True,
            ),
            dict(
                reviewed_commit=A,
                runtime_commit=A,
                ci_green_for_reviewed_commit=False,
                auto_deploy_disabled=True,
            ),
            dict(
                reviewed_commit=A,
                runtime_commit=A,
                ci_green_for_reviewed_commit=True,
                auto_deploy_disabled=False,
            ),
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(
                    assess_code_revision_pin(**case)[
                        "code_revision_verified"
                    ]
                )

    def test_invalid_sha_shape_blocks(self):
        for bad in ("", "abc", "A" * 40, "g" * 40, None):
            with self.subTest(bad=bad):
                r = assess_code_revision_pin(
                    reviewed_commit=bad,
                    runtime_commit=A,
                    ci_green_for_reviewed_commit=True,
                    auto_deploy_disabled=True,
                )
                self.assertFalse(r["code_revision_verified"])


if __name__ == "__main__":
    unittest.main()
