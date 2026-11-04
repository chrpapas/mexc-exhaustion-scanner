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
            "updateTime": int(time.time() * 1000),
        },
        "origin_deals": [{
            "id": "201",
            "orderId": "100",
            "symbol": "BTC_USDT",
            "side": 3,
            "vol": "1",
            "price": "100",
            "timestamp": int(time.time() * 1000),
        }],
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
        "volType": 2,
        "profitLossVolType": "SAME",
        "stopLossPrice": price,
        "stopLossType": 0,
        "stopLossReverse": 2,
        "lossTrend": 1,
    }


def close_deals(order_id="101", volume="1"):
    return [{
        "id": "200",
        "orderId": order_id,
        "symbol": "BTC_USDT",
        "side": 2,
        "vol": volume,
        "price": "100",
    }]


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
        self.p.observe(snapshot([stop("108")], flat=True), close_order=close, close_deals=close_deals())
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
                self.p.observe(snapshot(flat=True), close_order=o, close_deals=close_deals())
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
                if "/order/deal_details/" in path:
                    return snap["origin_deals"]
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
        self.p.observe(snapshot(flat=True), close_order=close, close_deals=close_deals())
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

    def test_amendment_cannot_silently_change_quantity_mode(self):
        self.protected()
        self.stage("AMEND", snapshot([stop()]), "108")
        for changes in ({"volType": 1}, {"profitLossVolType": "SEPARATE", "stopLossVol": "1"}):
            changed = dict(stop("108"), **changes)
            with self.subTest(changes=changes), self.assertRaises(PilotHalted):
                self.p.observe(snapshot([changed]))
            self.assertEqual(self.p.diagnostics()["state"], "AMEND_UNKNOWN")

    def flat_after_close(self):
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
        self.p.observe(snapshot([stop()], flat=True), close_order=close, close_deals=close_deals())

    def test_cancel_rejects_identity_and_terminal_state_drift(self):
        self.flat_after_close()
        for key, value in [
            ("symbol", "ETH_USDT"),
            ("orderId", 100),
            ("orderId", False),
            ("state", 3),
            ("isFinished", 1),
            ("triggerSide", 2),
        ]:
            bad = dict(stop(), **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(PilotHalted):
                self.stage("CANCEL", snapshot([bad], flat=True))
            self.assertEqual(self.p.diagnostics()["state"], "FLAT_OBSERVED")
        with self.assertRaises(PilotHalted):
            self.p.observe(
                snapshot(flat=True), canceled_stop=dict(stop(), state=2, isFinished=1, orderId=100)
            )

    def test_durable_write_failure_cannot_return_staged_request(self):
        self.p.db.execute(
            "CREATE TRIGGER fail_write BEFORE UPDATE ON pilot BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.stage("PLACE", snapshot(), "110")
        self.assertEqual(self.p.diagnostics()["state"], "NEW")
        self.assertEqual(self.p.diagnostics()["revision"], 0)
        self.p.db.execute("DROP TRIGGER fail_write")
        self.stage("PLACE", snapshot(), "110")
        self.p.close()
        self.p = self.open()
        with self.assertRaises(PilotHalted):
            self.stage("PLACE", snapshot(), "110")

    def test_observation_commit_failure_retains_unknown_after_restart(self):
        import sqlite3

        self.stage("PLACE", snapshot(), "110")
        self.p.db.execute(
            "CREATE TRIGGER fail_write BEFORE UPDATE ON pilot BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.p.observe(snapshot([stop()]), stop_id="99")
        self.p.close()
        self.p = self.open()
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")
        self.p.db.execute("DROP TRIGGER fail_write")
        self.p.observe(snapshot([stop()]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PROTECTED_OBSERVED")

    def test_missing_existing_journal_record_cannot_rearm(self):
        self.stage("PLACE", snapshot(), "110")
        self.p.db.execute("DELETE FROM pilot")
        with self.assertRaisesRegex(PilotHalted, "NO_REARM"):
            self.open()
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM pilot").fetchone()[0], 0)

    def test_existing_empty_file_is_not_a_new_experiment(self):
        empty = Path(self.temp.name) / "empty.sqlite"
        empty.touch()
        with self.assertRaisesRegex(PilotHalted, "NO_REARM"):
            PositionStopPilot(
                empty, account=ACCOUNT, origin_oid="pilot-origin", position_id="900", quantity="1"
            )

    def test_symlink_cannot_redirect_journal(self):
        link = Path(self.temp.name) / "link.sqlite"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(PilotHalted, "SYMLINK"):
            PositionStopPilot(
                link, account=ACCOUNT, origin_oid="pilot-origin", position_id="900", quantity="1"
            )


    def test_close_post_ack_id_must_equal_signed_get_order_after_restart(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        self.p.record_post_outcome(
            digest(request),
            {"success": True, "code": 0, "data": {"orderId": "101", "ts": 1793777069000}},
        )
        self.p.close()
        self.p = self.open()
        order = {
            "orderId": "102",
            "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 2,
            "dealVol": "1",
        }
        with self.assertRaisesRegex(PilotHalted, "CLOSE_ACK_GET_IDENTITY_MISMATCH"):
            self.p.observe(snapshot([stop()], flat=True), close_order=order, close_deals=close_deals())
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")
        order["orderId"] = "101"
        self.p.observe(snapshot([stop()], flat=True), close_order=order, close_deals=close_deals())
        self.assertEqual(self.p.diagnostics()["state"], "FLAT_OBSERVED")

    def test_conflicting_close_post_ack_ids_cannot_certify_flat(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        for identifier in ("101", "102", "101"):
            self.p.record_post_outcome(
                digest(request), {"success": True, "code": 0, "data": {"orderId": identifier}}
            )
        self.p.close()
        self.p = self.open()
        order = {
            "orderId": "101",
            "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 2,
            "dealVol": "1",
        }
        with self.assertRaisesRegex(PilotHalted, "CLOSE_ACK_IDENTITY_CONFLICT"):
            self.p.observe(snapshot([stop()], flat=True), close_order=order, close_deals=close_deals())
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")
        self.assertFalse(self.p.diagnostics()["live_authorization"])

    def test_close_post_bad_ack_shape_does_not_store_any_identifier_or_private_body(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        for data in (
            None, True, 101, "101", {"orderId": True},
            {"orderId": 0}, {"other": "PRIVATE_VALUE"},
        ):
            self.p.record_post_outcome(
                digest(request), {"success": True, "code": 0, "data": data}
            )
            row = self.p._read()
            self.assertEqual(row["events"][-1]["kind"], "AMBIGUOUS")
            self.assertNotIn("acknowledged_close_order_id", row)
            self.assertNotIn("PRIVATE_VALUE", str(row))
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")


    def test_close_requires_exact_trade_deal_history_and_keeps_unknown(self):
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        order = {
            "orderId": "101", "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT", "state": 3, "side": 2, "dealVol": "1",
        }
        bad = [
            None, [], [dict(close_deals()[0], orderId="102")],
            [dict(close_deals()[0], symbol="ETH_USDT")],
            [dict(close_deals()[0], side=3)],
            [dict(close_deals()[0], vol="0.5")],
            [dict(close_deals()[0], price=0)],
            [dict(close_deals()[0], id=True)],
            close_deals() + close_deals(),
        ]
        for evidence in bad:
            with self.subTest(evidence=evidence), self.assertRaises(PilotHalted):
                self.p.observe(snapshot([stop()], flat=True), close_order=order, close_deals=evidence)
            self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")
        self.p.observe(snapshot([stop()], flat=True), close_order=order, close_deals=close_deals())
        self.assertEqual(self.p.diagnostics()["state"], "FLAT_OBSERVED")

    def test_get_close_requires_two_matching_signed_trade_detail_reads(self):
        import asyncio
        from app.t100_live_readonly_probe import ReadOnlyMexcProbe
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        seen = {"deals": 0}
        current = snapshot([stop()], flat=True)
        closing = {
            "orderId": "101", "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT", "state": 3, "side": 2, "dealVol": "1",
        }

        class SignedGet:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return current["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return current["stops"]
                if path.endswith("/open_order_total_count"):
                    return current["counts"]
                if "/order/external/" in path:
                    return closing if request["payload"]["externalOid"] in path else current["origin"]
                if "/order/deal_details/100" in path:
                    return current["origin_deals"]
                if "/order/deal_details/101" in path:
                    seen["deals"] += 1
                    return close_deals()
                raise AssertionError("unexpected signed GET endpoint")

        asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(SignedGet())))
        self.assertEqual(seen["deals"], 2)
        self.assertEqual(self.p.diagnostics()["state"], "FLAT_OBSERVED")

    def test_get_close_deal_history_drift_fails_closed_without_post(self):
        import asyncio
        from app.t100_live_readonly_probe import ReadOnlyMexcProbe
        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        current = snapshot([stop()], flat=True)
        closing = {
            "orderId": "101", "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT", "state": 3, "side": 2, "dealVol": "1",
        }
        reads = {"n": 0}

        class SignedGet:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return current["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return current["stops"]
                if path.endswith("/open_order_total_count"):
                    return current["counts"]
                if "/order/external/" in path:
                    return closing if request["payload"]["externalOid"] in path else current["origin"]
                if "/order/deal_details/100" in path:
                    return current["origin_deals"]
                if "/order/deal_details/101" in path:
                    reads["n"] += 1
                    return close_deals(volume="1" if reads["n"] == 1 else "0.5")
                raise AssertionError("unexpected signed GET endpoint")

        with self.assertRaisesRegex(PilotHalted, "^SIGNED_GET_RECONCILIATION_UNVERIFIED$"):
            asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(SignedGet())))
        self.assertEqual(reads["n"], 2)
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")
        self.assertFalse(self.p.diagnostics()["post_transport_available"])



    def test_place_review_rejects_stale_or_unreliable_fill_timestamp(self):
        snap = snapshot()
        values = [
            (None, "TIMESTAMP_UNVERIFIED"),
            (True, "TIMESTAMP_UNVERIFIED"),
            ("179", "TIMESTAMP_UNVERIFIED"),
            (0, "TIMESTAMP_UNVERIFIED"),
            (int(time.time() * 1000) - 60_000, "TOO_OLD"),
            (int(time.time() * 1000) + 60_000, "CLOCK_SKEW"),
        ]
        for value, failure in values:
            candidate = snapshot()
            candidate["origin"]["updateTime"] = value
            with self.subTest(value=value), self.assertRaisesRegex(PilotHalted, failure):
                self.p.proposal("PLACE", candidate, price="110")
            # Bad/aged native-placement review must NOT block a
            # separately authorized one-shot emergency close proposal.
            emergency = self.p.proposal("CLOSE", candidate)
            self.assertEqual(emergency["action"], "CLOSE")
        self.assertEqual(self.p.diagnostics()["state"], "NEW")

    def test_place_journal_preserves_fill_time_without_certifying_gap(self):
        candidate = snapshot()
        request = self.p.proposal("PLACE", candidate, price="110")
        self.p.stage(
            request, candidate,
            Approval(ACCOUNT, digest(request), time.time() + 30,
                     "REVIEW_ONLY_POSITION_STOP_PILOT"),
        )
        data = self.p._read()
        self.assertEqual(
            data["place_review_deadline_ms"] - data["entry_first_fill_ms"],
            15_000,
        )
        self.assertEqual(self.p.diagnostics()["place_review_deadline_expired"], False)
        self.assertFalse(self.p.diagnostics()["protection_gap_acceptable"])
        self.assertFalse(self.p.diagnostics()["entry_allowed"])
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")
        with self.assertRaises(PilotHalted):
            self.stage("PLACE", snapshot(), "110")


    def test_place_requests_documented_whole_position_stop_mode(self):
        request = self.p.proposal("PLACE", snapshot(), price="110")
        self.assertEqual(request["path"], "/api/v1/private/stoporder/place")
        self.assertEqual(request["payload"]["volType"], 2)
        self.assertEqual(request["payload"]["profitLossVolType"], "SAME")
        self.assertEqual(request["payload"]["vol"], "1")
        self.assertEqual(request["payload"]["stopLossType"], 0)
        self.assertEqual(request["payload"]["stopLossReverse"], 2)
        self.assertFalse(self.p.diagnostics()["live_authorization"])

    def test_partial_mode_never_counts_as_entire_position_protection(self):
        self.stage("PLACE", snapshot(), "110")
        row = stop()
        row["volType"] = 1
        with self.assertRaisesRegex(PilotHalted, "STOP_QUANTITY_MODE_CHANGED"):
            self.p.observe(snapshot([row]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")

    def test_whole_position_get_zero_or_omitted_volume_still_requires_native_fields(self):
        for reported in (None, "0", 0, "1"):
            with self.subTest(reported=reported):
                from tempfile import TemporaryDirectory
                with TemporaryDirectory() as tmp:
                    pilot = PositionStopPilot(
                        Path(tmp) / "scope.sqlite", account=ACCOUNT,
                        origin_oid="pilot-origin", position_id="900", quantity="1",
                    )
                    try:
                        snap = snapshot()
                        request = pilot.proposal("PLACE", snap, price="110")
                        pilot.stage(
                            request, snap,
                            Approval(ACCOUNT, digest(request), time.time() + 30,
                                     "REVIEW_ONLY_POSITION_STOP_PILOT"),
                        )
                        row = stop()
                        if reported is None:
                            row.pop("vol")
                        else:
                            row["vol"] = reported
                        pilot.observe(snapshot([row]), stop_id="99")
                        self.assertEqual(pilot.diagnostics()["state"], "PROTECTED_OBSERVED")
                        self.assertFalse(pilot.diagnostics()["stop_execution_certified"])
                    finally:
                        pilot.close()

    def test_whole_position_get_inconsistent_volume_rejected(self):
        self.stage("PLACE", snapshot(), "110")
        for reported in (False, "-1", "NaN", "2", "0.5", [], {}):
            row = stop()
            row["vol"] = reported
            with self.subTest(reported=reported), self.assertRaises(PilotHalted):
                self.p.observe(snapshot([row]), stop_id="99")
            self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")


    def test_place_refuses_late_first_fill_despite_recent_order_update(self):
        latest = snapshot()
        latest["origin"]["updateTime"] = int(time.time() * 1000)
        latest["origin_deals"][0]["timestamp"] -= 60_000
        with self.assertRaisesRegex(PilotHalted, "TOO_OLD"):
            self.p.proposal("PLACE", latest, price="110")
        self.assertEqual(self.p.proposal("CLOSE", latest)["action"], "CLOSE")
        self.assertEqual(self.p.diagnostics()["state"], "NEW")

    def test_place_requires_matching_unique_complete_signed_trade_details(self):
        wrong = [
            [],
            [dict(snapshot()["origin_deals"][0], orderId="999")],
            [dict(snapshot()["origin_deals"][0], side=2)],
            [dict(snapshot()["origin_deals"][0], vol="0.5")],
            [dict(snapshot()["origin_deals"][0], price=0)],
            [dict(snapshot()["origin_deals"][0], timestamp=None)],
            snapshot()["origin_deals"] * 2,
        ]
        for trades in wrong:
            candidate = snapshot()
            candidate["origin_deals"] = trades
            with self.subTest(trades=trades), self.assertRaises(PilotHalted):
                self.p.proposal("PLACE", candidate, price="110")
            self.assertEqual(self.p.diagnostics()["state"], "NEW")

    def test_native_stop_arriving_after_deadline_cannot_mark_protected(self):
        proposal = self.stage("PLACE", snapshot(), "110")
        row = self.p._read()
        row["place_review_deadline_ms"] = 1
        self.p.db.execute(
            "UPDATE pilot SET body=? WHERE id=1", (__import__("json").dumps(row),)
        )
        with self.assertRaisesRegex(PilotHalted, "PROTECTION_GAP_DEADLINE_EXCEEDED"):
            self.p.observe(snapshot([stop()]), stop_id="99")
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")
        self.assertEqual(self.p.diagnostics()["place_review_deadline_expired"], True)
        self.assertEqual(self.p.proposal("CLOSE", snapshot([stop()]))["action"], "CLOSE")
        self.assertFalse(self.p.diagnostics()["live_authorization"])

    def test_preflight_snapshot_requires_two_stable_entry_trade_reads(self):
        import asyncio
        from app.t100_live_readonly_probe import ReadOnlyMexcProbe
        from app.t100_live_position_stop_pilot import collect_get_snapshot
        reference = snapshot()
        reads = {"n": 0}

        class GetOnly:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return reference["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return reference["stops"]
                if path.endswith("/open_order_total_count"):
                    return reference["counts"]
                if "/order/external/" in path:
                    return reference["origin"]
                if "/order/deal_details/" in path:
                    reads["n"] += 1
                    if reads["n"] == 2:
                        return [dict(reference["origin_deals"][0], vol="0.5")]
                    return reference["origin_deals"]
                raise AssertionError("unexpected GET route")

        with self.assertRaisesRegex(PilotHalted, "SIGNED_GET_SNAPSHOT_UNVERIFIED"):
            asyncio.run(collect_get_snapshot(ReadOnlyMexcProbe(GetOnly()), ACCOUNT, "pilot-origin"))
        self.assertEqual(reads["n"], 2)

    def test_acknowledged_placement_id_binds_get_after_restart(self):
        request = self.stage("PLACE", snapshot(), "110")
        self.p.record_post_outcome(digest(request), {"success": True, "code": 0, "data": "99"})
        self.p.close()
        self.p = self.open()
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot([dict(stop(), id="100")]), stop_id="100")
        self.assertEqual(self.p.diagnostics()["state"], "PLACE_UNKNOWN")
        self.p.observe(snapshot([stop()]))
        self.assertEqual(self.p.diagnostics()["state"], "PROTECTED_OBSERVED")

    def test_conflicting_placement_acknowledgements_latch_investigation(self):
        request = self.stage("PLACE", snapshot(), "110")
        for identifier in ["99", "100", "99"]:
            self.p.record_post_outcome(digest(request), {"success": True, "code": 0, "data": identifier})
        self.p.close()
        self.p = self.open()
        with self.assertRaisesRegex(PilotHalted, "IDENTITY_CONFLICT"):
            self.p.observe(snapshot([stop()]), stop_id="99")
        self.stage("CLOSE", snapshot([stop()]))
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")

    def test_malformed_placement_acknowledgement_never_guesses_identity(self):
        request = self.stage("PLACE", snapshot(), "110")
        for value in [None, True, 0, {"id": "99"}, [{"id": "99"}]]:
            self.p.record_post_outcome(digest(request), {"success": True, "code": 0, "data": value})
            self.assertNotIn("acknowledged_stop_id", self.p._read())
            self.assertEqual(self.p._read()["events"][-1]["kind"], "AMBIGUOUS")
        with self.assertRaises(PilotHalted):
            self.p.observe(snapshot([stop()]))


    def test_get_close_rejects_entry_deal_drift_across_inventory_bookends(self):
        """An individually stable signed entry ledger may still change between bookends."""
        import asyncio

        from app.t100_live_readonly_probe import ReadOnlyMexcProbe

        self.protected()
        request = self.stage("CLOSE", snapshot([stop()]))
        current = snapshot([stop()], flat=True)
        closing = {
            "orderId": "101",
            "positionId": "900",
            "externalOid": request["payload"]["externalOid"],
            "symbol": "BTC_USDT",
            "state": 3,
            "side": 2,
            "dealVol": "1",
        }
        entry_deal_reads = {"count": 0}

        class SignedGet:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return current["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return current["stops"]
                if path.endswith("/open_order_total_count"):
                    return current["counts"]
                if "/order/external/" in path:
                    if request["payload"]["externalOid"] in path:
                        return closing
                    return current["origin"]
                if "/order/deal_details/100" in path:
                    entry_deal_reads["count"] += 1
                    # Same ledger within each two-read snapshot, changed
                    # only across outer reconciliation bookends.
                    trade = dict(current["origin_deals"][0])
                    if entry_deal_reads["count"] > 2:
                        trade["price"] = "101"
                    return [trade]
                if "/order/deal_details/101" in path:
                    return close_deals()
                raise AssertionError("unexpected signed GET endpoint")

        with self.assertRaisesRegex(
            PilotHalted, "^SIGNED_GET_RECONCILIATION_UNVERIFIED$"
        ):
            asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(SignedGet())))
        self.assertEqual(entry_deal_reads["count"], 4)
        self.assertEqual(self.p.diagnostics()["state"], "CLOSE_UNKNOWN")
        self.assertFalse(self.p.diagnostics()["live_authorization"])


    def test_cancel_get_rejects_terminal_stop_history_drift_between_reads(self):
        """A single terminal-row witness cannot establish settled cancellation."""
        import asyncio

        from app.t100_live_readonly_probe import ReadOnlyMexcProbe

        self.flat_after_close()
        self.stage("CANCEL", snapshot([stop()], flat=True))
        current = snapshot(flat=True)
        history_reads = {"n": 0}

        class SignedGet:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return current["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return current["stops"]
                if path.endswith("/open_order_total_count"):
                    return current["counts"]
                if "/order/external/" in path:
                    return current["origin"]
                if "/order/deal_details/100" in path:
                    return current["origin_deals"]
                if path.endswith("/stoporder/list/orders"):
                    history_reads["n"] += 1
                    terminal = dict(stop(), state=2, isFinished=1)
                    if history_reads["n"] == 2:
                        terminal["triggerSide"] = 1
                    return [terminal]
                raise AssertionError("unexpected signed GET endpoint")

        with self.assertRaisesRegex(
            PilotHalted, "^SIGNED_GET_RECONCILIATION_UNVERIFIED$"
        ):
            asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(SignedGet())))
        self.assertEqual(history_reads["n"], 2)
        self.assertEqual(self.p.diagnostics()["state"], "CANCEL_UNKNOWN")
        self.assertFalse(self.p.diagnostics()["post_transport_available"])


    def test_cancel_get_requires_two_matching_terminal_history_reads(self):
        """Stable authenticated terminal history may complete offline recovery."""
        import asyncio

        from app.t100_live_readonly_probe import ReadOnlyMexcProbe

        self.flat_after_close()
        self.stage("CANCEL", snapshot([stop()], flat=True))
        current = snapshot(flat=True)
        history_reads = {"n": 0}

        class SignedGet:
            async def _private_get(self, path, params=None):
                if path.endswith("/position/open_positions"):
                    return current["positions"]
                if path.endswith("/stoporder/open_orders"):
                    return current["stops"]
                if path.endswith("/open_order_total_count"):
                    return current["counts"]
                if "/order/external/" in path:
                    return current["origin"]
                if "/order/deal_details/100" in path:
                    return current["origin_deals"]
                if path.endswith("/stoporder/list/orders"):
                    history_reads["n"] += 1
                    return [dict(stop(), state=2, isFinished=1)]
                raise AssertionError("unexpected signed GET endpoint")

        asyncio.run(self.p.observe_get(ReadOnlyMexcProbe(SignedGet())))
        self.assertEqual(history_reads["n"], 2)
        self.assertEqual(self.p.diagnostics()["state"], "FLAT_STOP_CANCELED_OBSERVED")
        self.assertFalse(self.p.diagnostics()["stop_execution_certified"])
        self.assertFalse(self.p.diagnostics()["live_authorization"])
