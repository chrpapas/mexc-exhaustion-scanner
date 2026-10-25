"""Static safety checks for the draft Render live-shadow blueprint."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "render.t100-live-shadow.draft.yaml"


class ShadowBlueprintTests(unittest.TestCase):
    def test_blueprint_exists_and_targets_shadow_runtime(self):
        text = BLUEPRINT.read_text()
        self.assertIn("name: mexc-t100-live-shadow", text)
        self.assertIn(
            "startCommand: python -m app.t100_live_shadow_runtime",
            text,
        )
        self.assertIn("region: frankfurt", text)

    def test_auto_deploy_is_off(self):
        text = BLUEPRINT.read_text()
        self.assertIn("autoDeployTrigger: off", text)
        self.assertNotIn("autoDeployTrigger: commit", text)
        self.assertNotIn("autoDeployTrigger: checksPass", text)

    def test_execution_boundary_is_explicitly_non_authorizing(self):
        text = BLUEPRINT.read_text()
        self.assertRegex(
            text,
            r"(?s)key: T100_EXECUTION_MODE\s+value: paper",
        )
        self.assertRegex(
            text,
            r'(?s)key: T100_LIVE_ORDER_API_ENABLED\s+value: "false"',
        )
        self.assertRegex(
            text,
            r'(?s)key: T100_LIVE_KILL_SWITCH\s+value: "true"',
        )

    def test_shadow_worker_has_no_database_dependency(self):
        text = BLUEPRINT.read_text()
        self.assertNotIn("DATABASE_URL", text)
        self.assertNotIn("fromDatabase:", text)

    def test_private_values_are_declared_as_unsynced_secrets(self):
        text = BLUEPRINT.read_text()
        for key in (
            "MEXC_API_KEY",
            "MEXC_API_SECRET",
            "DISCORD_TRADER_EVENTS_WEBHOOK_URL",
        ):
            self.assertRegex(
                text,
                rf'(?s)key: {key}\s+sync: false',
            )

    def test_discord_is_required(self):
        text = BLUEPRINT.read_text()
        self.assertRegex(
            text,
            r'(?s)key: T100_SHADOW_REQUIRE_DISCORD\s+value: "true"',
        )


if __name__ == "__main__":
    unittest.main()
