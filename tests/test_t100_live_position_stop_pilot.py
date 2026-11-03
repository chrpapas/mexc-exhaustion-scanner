import tempfile
import time
import unittest
from pathlib import Path

from app.t100_live_position_stop_pilot import Approval, PilotHalted, PositionStopPilot, digest

ACCOUNT = "a" * 64


def snapshot(stops=None, flat=False):
    stops = stops or []
    return {
        "account": ACCOUNT,
        "observed_at": time.time(),
        "positions": []
        if flat
        else [
            {
                "positionId": "900",
                "symbol": "BTC_USDT",
                "holdVol": "1",
                "positionType": 2,
                "openType": 1,
                "leverage": 1,
            }
        ],
        "origin": {
            "orderId": "100",
            "positionId": "900",
            "externalOid": "pilot-origin",
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 3,
            "dealVol": "1",
        },
        "stops": stops,
        "counts": {
            "sumCount": len(stops),
            "stopOrderCount": len(stops),
            "limitOrderCount": 0,
            "planOrderCount": 0,
            "trackOrderCount": 0,
        },
    }


def stop(price="110"):
    return {
        "id": "99",
        "positionId": "900",
        "symbol": "BTC_USDT",
        "orderId": 0,
        "state": 1,
        "isFinished": 0,
        "triggerSide": 0,
        "errorCode": 0,
        "positionType": 2,
        "vol": "1",
        "volType": 1,
        "profitLossVolType": "SAME",
        "stopLossPrice": price,
        "stopLossType": 0,
        "stopLossReverse": 2,
        "lossTrend": 1,
    }


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.p = self.open()

    def open(self):
        return PositionStopPilot(
            self.path, account=ACCOUNT, origin_oid="pilot-origin", position_id="900", quantity="1"
        )

    def tearDown(self):
        self.p.close()
        self.temp.cleanup()

    def stage(self, action, snap, price=None):
        request = self.p.proposal(action, snap, price=price)
        approval = Approval(ACCOUNT, digest(request), time.time() + 30, "REVIEW_ONLY_POSITION_STOP_PILOT")
        self.p.stage(request, snap, approval)
        return request

    def protected(self):
        self.stage("PLACE", snapshot(), "110")
        self.p.observe(snapshot([stop()]), stop_id="99")

    def test_place_amend_close_cancel_offline_chain(self):
        self.protected()
        self.stage("AMEND", snapshot([stop()]), "108")
        self.p.observe(snapshot([stop("108")]))
        request = self.stage("CLOSE", snapshot([stop("108")]))
        close = {
            "orderId": "101",
            "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 2,
            "dealVol": "1",
        }
        self.p.observe(snapshot([stop("108")], flat=True), close_order=close)
        self.stage("CANCEL", snapshot([stop("108")], flat=True))
        terminal = dict(stop("108"), state=2, isFinished=1)
        self.p.observe(snapshot(flat=True), canceled_stop=terminal)
        d = self.p.diagnostics()
        self.assertEqual(d["state"], "FLAT_STOP_CANCELED_OBSERVED")
        self.assertFalse(d["live_authorization"])
        self.assertFalse(d["stop_execution_certified"])
        self.assertNotIn("900", str(d))
        with self.assertRaises(PilotHalted):
            self.p.dispatch()

    def test_crash_after_durable_stage_cannot_retry(self):
        self.stage("PLACE", snapshot(), "110")
        self.p.close()
        self.p = self.open()
        self.assertTrue(self.p.diagnostics()["reconciliation_required"])
        with self.assertRaises(PilotHalted):
            self.stage("PLACE", snapshot(), "110")
        self.p.observe(snapshot([stop()]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PROTECTED_OBSERVED")

    def test_operator_scope_and_snapshot_are_required_before_journal_change(self):
        snap = snapshot()
        request = self.p.proposal("PLACE", snap, price="110")
        for approval in [
            None,
            Approval(ACCOUNT, "wrong", time.time() + 30, "REVIEW_ONLY_POSITION_STOP_PILOT"),
            Approval(ACCOUNT, digest(request), time.time() - 1, "REVIEW_ONLY_POSITION_STOP_PILOT"),
        ]:
            with self.subTest(approval=approval), self.assertRaises(PilotHalted):
                self.p.stage(request, snap, approval)
        self.assertEqual(self.p.diagnostics()["state"], "NEW")
        snap["observed_at"] -= 10
        with self.assertRaises(PilotHalted):
            self.p.proposal("PLACE", snap, price="110")

    def test_real_order_attached_missing_type_evidence_is_rejected(self):
        self.stage("PLACE", snapshot(), "110")
        bad = stop()
        bad.pop("stopLossType")
        bad["orderId"] = 100
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot([bad]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")

    def test_mutated_stop_fields_do_not_advance_state(self):
        self.stage("PLACE", snapshot(), "110")
        for key, value in [
            ("stopLossType", None),
            ("orderId", 1),
            ("vol", "0.5"),
            ("triggerSide", 2),
            ("state", True),
            ("positionId", "901"),
            ("stopLossPrice", "109"),
            ("stopLossReverse", 1),
        ]:
            row = stop()
            row[key] = value
            with self.subTest(key=key), self.assertRaises(PilotHalted):
                self.p.observe(snapshot([row]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["revision"], 1)

    def test_no_cancel_first_no_looser_amendment_no_wrong_account(self):
        self.protected()
        for action, price in [("CANCEL", None), ("AMEND", "111")]:
            with self.assertRaises(PilotHalted):
                self.p.proposal(action, snapshot([stop()]), price=price)
        snap = snapshot([stop()])
        snap["account"] = "b" * 64
        with self.assertRaises(PilotHalted):
            self.p.proposal("CLOSE", snap)

    def test_partial_close_and_absence_alone_never_prove_closed(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        for o in [
            None,
            {
                "orderId": "101",
                "positionId": "900",
                "externalOid": request["payload"]["externalOid"],
                "symbol": "BTC_USDT",
                "state": 3,
                "side": 2,
                "dealVol": "0.5",
            },
        ]:
            with self.assertRaises(PilotHalted):
                self.p.observe(snapshot(flat=True), close_order=o)
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")

    def test_two_processes_cannot_stage_same_request(self):
        other = self.open()
        try:
            snap = snapshot()
            request = other.proposal("PLACE", snap, price="110")
            self.stage("PLACE", snap, "110")
            with self.assertRaises(PilotHalted):
                other.stage(
                    request,
                    snap,
                    Approval(ACCOUNT, digest(request), time.time() + 30, "REVIEW_ONLY_POSITION_STOP_PILOT"),
                )
        finally:
            other.close()

    def test_signed_get_adapter_and_transport_error_redaction(self):
        import asyncio

        from app.t100_live_readonly_probe import ReadOnlyMexcProbe

        self.stage("PLACE", snapshot(), "110")
        snap = snapshot([stop()])

        class GetOnly:
            def __init__(self):
                self.paths = []
                self.fail = False

            async def _private_get(self, path, params=None):
                self.paths.append(path)
                if self.fail:
                    raise RuntimeError("SENSITIVE_PRIVATE_RESPONSE")
                if path.endswith("/position/open_positions"):
                    return snap["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return snap["stops"]
                if path.endswith("/open_order_total_count"):
                    return snap["counts"]
                if "/order/external/" in path:
                    return snap["origin"]
                raise AssertionError(path)

        client = GetOnly()
        asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(client), stop_id="99"))
        self.assertEqual(self.p.diagnostics()["state"], "PROTECTED_OBSERVED")
        self.assertTrue(client.paths)
        self.stage("AMEND", snapshot([stop()]), "108")
        client.fail = True
        with self.assertRaisesRegex(PilotHalted, "^SIGNED_GET_RECONCILIATION_UNVERIFIED$"):
            asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(client)))
        self.assertEqual(self.p.diagnostics()["state"], "AMEND_UNKNOWN")

    def test_automatic_cancel_after_flat_requires_terminal_identity(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        close = {
            "orderId": "101",
            "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 2,
            "dealVol": "1",
        }
        self.p.observe(snapshot(flat=True), close_order=close)
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot(flat=True), canceled_stop=None)
        self.p.observe(snapshot(flat=True), canceled_stop=dict(stop(), state=2, isFinished=1))
        self.assertEqual(self.p.diagnostics()["state"], "FLAT_STOP_CANCELED_OBSERVED")

    def test_post_responses_never_authorize_retry_or_protection(self):
        request = self.stage("PLACE", snapshot(), "110")
        for response in [
            None,
            {},
            {"success": True, "code": True},
            {"success": False, "code": 2030, "data": None, "message": "PRIVATE"},
            {"success": True, "code": 0, "data": "99"},
        ]:
            self.p.record_post_outcome(digest(request), response)
            self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")
            with self.assertRaises(PilotHalted):
                self.stage("PLACE", snapshot(), "110")
        self.assertNotIn("PRIVATE", str(self.p._read()))
        self.p.close()
        self.p = self.open()
        self.p.observe(snapshot([stop()]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PROTECTED_OBSERVED")

    def test_emergency_close_before_first_stop_and_no_duplicate_close(self):
        self.stage("CLOSE", snapshot())
        with self.assertRaises(PilotHalted):
            self.stage("CLOSE", snapshot())
        self.assertFalse(self.p.diagnostics()["protection_gap_acceptable"])

    def test_unknown_amend_old_price_stays_unknown_and_close_preserves_attempt(self):
        self.protected()
        amendment = self.stage("AMEND", snapshot([stop()]), "108")
        self.p.record_post_outcome(digest(amendment), {"success": False, "code": 1, "data": None})
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot([stop()]))
        self.assertEqual(self.p.diagnostics()["state"], "AMEND_UNKNOWN")
        with self.assertRaises(PilotHalted):
            self.p.record_post_outcome("wrong", None)
        self.stage("CLOSE", snapshot([stop()]))
        self.assertTrue(
            any(
                e.get("request") == amendment and e["kind"] == "SUPERSEDED_PENDING"
                for e in self.p._read()["events"]
            )
        )

    def test_stale_get_revision_cannot_commit(self):
        self.stage("PLACE", snapshot(), "110")
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot([stop()]), stop_id="99", expected_revision=0)
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")

    def test_ephemeral_journal_is_rejected(self):
        for path in ["", ":memory:"]:
            with self.subTest(path=path), self.assertRaises(PilotHalted):
                PositionStopPilot(
                    path, account=ACCOUNT, origin_oid="pilot-origin", position_id="900", quantity="1"
                )

    def test_one_contract_limit_cannot_be_expanded(self):
        with self.assertRaisesRegex(PilotHalted, "PILOT_ONE_CONTRACT_LIMIT"):
            PositionStopPilot(
                self.path, account=ACCOUNT, origin_oid="pilot-origin", position_id="900", quantity="2"
            )
