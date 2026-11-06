"""Offline safety audit for separate REVIEW-ONLY Render observer Blueprint.

No calls to Render, exchange, or PostgreSQL.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

from app.t100_live_guardian_readonly_worker import run_worker
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "t100_guardian_observer.render.REVIEW_ONLY.yaml"


class SafeDeploymentReviewTests(unittest.TestCase):
    def test_blueprint_is_a_nonroot_review_artifact_with_no_autodeploy(self):
        self.assertTrue(MANIFEST.is_file())
        self.assertNotEqual(MANIFEST.name,"render.yaml")
        s=MANIFEST.read_text()
        self.assertIn("REVIEW ONLY",s.splitlines()[0])
        self.assertEqual(s.count("  - type: worker"),1)
        self.assertIn("autoDeployTrigger: off",s)
        self.assertNotIn("autoDeployTrigger: commit",s)
        self.assertNotIn("autoDeployTrigger: checksPass",s)
        self.assertNotIn("  - type: web",s)
        self.assertNotIn("  - type: cron",s)
        # Comments can mention the old service as a warning; only actual
        # service declarations could accidentally overwrite it.
        self.assertNotRegex(s,r"(?m)^\\s+name:\\s+mexc-t100-live-shadow\\s*$")
        self.assertRegex(
            s,r"(?m)^\\s+name:\\s+mexc-t100-guardian-observer-review-only\\s*$"
        )
        self.assertNotIn("fromDatabase:",s)
        self.assertNotIn("fromService:",s)
        self.assertNotIn("plan:",s)

    def test_no_live_flags_enabled_or_credential_values_committed(self):
        s=MANIFEST.read_text()
        for name,value in (
            ("T100_GUARDIAN_WORKER_ENABLED","false"),
            ("T100_GUARDIAN_OBSERVER_ENABLED","false"),
            ("T100_EXECUTION_MODE","paper"),
            ("T100_LIVE_ORDER_API_ENABLED","false"),
            ("T100_LIVE_KILL_SWITCH","true"),
        ):
            with self.subTest(name=name):
                self.assertRegex(
                    s,rf"- key: {name}\n\s+value: [\"']?{value}[\"']?(?:\n|$)"
                )
        for name in (
            "T100_GUARDIAN_MEXC_API_KEY",
            "T100_GUARDIAN_MEXC_API_SECRET",
            "T100_GUARDIAN_ACCOUNT_KEY_SHA256",
            "T100_GUARDIAN_POSITION_ID",
            "T100_GUARDIAN_READONLY_DB_DSN",
            "T100_GUARDIAN_DB_CA_FILE",
        ):
            with self.subTest(name=name):
                self.assertRegex(s,rf"- key: {name}\n\s+sync: false")
                self.assertNotRegex(s,rf"- key: {name}\n\s+value:")

    def test_worker_entrypoint_is_read_only_runtime_only(self):
        s=MANIFEST.read_text()
        self.assertIn(
            "startCommand: python -m app.t100_live_guardian_readonly_worker --run",s)
        self.assertTrue(callable(run_worker))
        self.assertFalse(hasattr(GetOnlyMexcTransport,"_private_post"))


if __name__ == "__main__":
    unittest.main()
