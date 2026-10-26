"""Offline phase-specific private WS readiness tests."""
import unittest

from app.t100_live_ws_readiness_gate import (
    assess_ws_full_lifecycle_promotion,
    assess_ws_pre_pilot_contract,
)


def ws_report(**kw):
    data = {
        "read_only": True,
        "login_verified": True,
        "filter_sent": True,
        "status": "COMPLETED_PRIVATE_WS_OBSERVATION",
        "unexpected_private_channels": [],
        "private_events_seen": 0,
        "payload_semantic_events_verified": 0,
        "payload_semantic_failures": 0,
    }
    data.update(kw)
    return data


class WsReadinessGateTests(unittest.TestCase):
    def test_zero_event_real_login_can_pass_pre_pilot_contract(self):
        r = assess_ws_pre_pilot_contract(
            ws_report(),
            semantic_parser_contract_verified=True,
        )
        self.assertTrue(
            r["private_ws_pre_pilot_contract_verified"]
        )
        self.assertFalse(r["private_ws_full_lifecycle_verified"])
        self.assertFalse(r["live_authorization"])

    def test_observed_events_must_all_pass_semantic_checks(self):
        good = assess_ws_pre_pilot_contract(
            ws_report(
                private_events_seen=2,
                payload_semantic_events_verified=2,
            ),
            semantic_parser_contract_verified=True,
        )
        self.assertTrue(
            good["private_ws_pre_pilot_contract_verified"]
        )

        bad = assess_ws_pre_pilot_contract(
            ws_report(
                private_events_seen=2,
                payload_semantic_events_verified=1,
                payload_semantic_failures=1,
            ),
            semantic_parser_contract_verified=True,
        )
        self.assertFalse(
            bad["private_ws_pre_pilot_contract_verified"]
        )

    def test_parser_login_filter_or_unexpected_channel_blocks(self):
        cases = (
            (ws_report(), False),
            (ws_report(login_verified=False), True),
            (ws_report(filter_sent=False), True),
            (
                ws_report(
                    unexpected_private_channels=[
                        "push.personal.asset"
                    ]
                ),
                True,
            ),
        )
        for report, parser_ok in cases:
            with self.subTest(report=report, parser_ok=parser_ok):
                self.assertFalse(
                    assess_ws_pre_pilot_contract(
                        report,
                        semantic_parser_contract_verified=parser_ok,
                    )["private_ws_pre_pilot_contract_verified"]
                )

    def test_full_lifecycle_requires_all_three_correlated_channels(self):
        reports = [
            {
                "channel": channel,
                "lifecycle_semantics_verified": True,
            }
            for channel in (
                "push.personal.position",
                "push.personal.stop.planorder",
                "push.personal.order.deal",
            )
        ]
        r = assess_ws_full_lifecycle_promotion(reports)
        self.assertTrue(r["private_ws_full_lifecycle_verified"])
        self.assertFalse(r["live_authorization"])

    def test_missing_or_failed_lifecycle_channel_blocks_promotion(self):
        missing = assess_ws_full_lifecycle_promotion([
            {
                "channel": "push.personal.position",
                "lifecycle_semantics_verified": True,
            },
            {
                "channel": "push.personal.stop.planorder",
                "lifecycle_semantics_verified": True,
            },
        ])
        self.assertFalse(
            missing["private_ws_full_lifecycle_verified"]
        )

        failed = assess_ws_full_lifecycle_promotion([
            {
                "channel": "push.personal.position",
                "lifecycle_semantics_verified": True,
            },
            {
                "channel": "push.personal.stop.planorder",
                "lifecycle_semantics_verified": True,
            },
            {
                "channel": "push.personal.order.deal",
                "lifecycle_semantics_verified": False,
            },
        ])
        self.assertFalse(
            failed["private_ws_full_lifecycle_verified"]
        )


if __name__ == "__main__":
    unittest.main()
