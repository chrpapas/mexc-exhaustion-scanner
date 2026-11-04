"""Separate offline pilot lifecycle. GET collection only; no POST transport.

A staged request is a review artifact, never a mutation permit. Reopening an
unresolved journal cannot restage a request. No production module imports this.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.t100_live_native_stop_execution_audit import assess_native_short_market_stop_row
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class PilotHalted(RuntimeError):
    pass


# Review-age ceiling, NOT an execution protection deadline or a loss cap.
# A later real executor needs its own independent watchdog/fill timestamp.
MAX_ENTRY_FILL_AGE_FOR_PLACE_REVIEW_MS = 15_000
MAX_EXCHANGE_CLOCK_LEAD_MS = 2_000


def number(value):
    try:
        if type(value) is bool:
            raise ValueError
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0:
            raise ValueError
        return n
    except (ValueError, TypeError, InvalidOperation):
        raise PilotHalted("INVALID_NUMBER") from None


def identity(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise PilotHalted("INVALID_IDENTITY")
    return str(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Approval:
    """Offline review scope, not live authorization or a cryptographic credential."""

    account: str
    request_sha256: str
    expires_at: float
    phrase: str


async def collect_get_snapshot(probe: ReadOnlyMexcProbe, account: str, origin_oid: str):
    """Caller binds account fingerprint to its signed GET client. Never reads keys.

    Bookended inventory is non-atomic. Strict failure does not prove no fill.
    Exceptions are redacted; private response bodies never enter diagnostics.
    """
    if type(probe) is not ReadOnlyMexcProbe:
        raise PilotHalted("SIGNED_GET_PROBE_REQUIRED")
    started = time.time()
    try:

        async def inventory():
            return {
                "positions": await probe.raw_open_positions(),
                "stops": await probe.raw_open_stops(),
                "counts": await probe.raw_open_order_counts(),
            }

        before = await inventory()
        origin = await probe.raw_order_by_external_oid("BTC_USDT", origin_oid)
        if type(origin) is not dict:
            raise PilotHalted("ENTRY_ORIGIN_GET_UNVERIFIED")
        order_id = identity(origin.get("orderId"))
        origin_deals = await probe.raw_order_deal_details(order_id)
        after = await inventory()
        origin_bookend = await probe.raw_order_by_external_oid("BTC_USDT", origin_oid)
        origin_deals_bookend = await probe.raw_order_deal_details(order_id)
        if (
            before != after or origin != origin_bookend
            or origin_deals != origin_deals_bookend
        ):
            raise PilotHalted("ENTRY_OR_INVENTORY_CHANGED")
        return dict(
            after, origin=origin, origin_deals=origin_deals,
            account=account, observed_at=started,
        )
    except Exception:  # noqa: BLE001 - redact all transport exception bodies
        raise PilotHalted("SIGNED_GET_SNAPSHOT_UNVERIFIED") from None


class PositionStopPilot:
    """Local durable single-experiment journal; one account/position, no adoption.

    SQLITE FULL synchronous transactions precede returning any request. Only
    stage/observe are exposed; this class cannot execute requests. A future
    supervised executor needs separate review and explicit operator approval.
    """

    def __init__(self, path, *, account, origin_oid, position_id, quantity):
        if str(path) in ("", ":memory:"):
            raise PilotHalted("DURABLE_JOURNAL_REQUIRED")
        journal_path = Path(path)
        if journal_path.is_symlink():
            raise PilotHalted("JOURNAL_SYMLINK_REJECTED")
        existing_journal = journal_path.exists()
        if not re.fullmatch(r"[a-f0-9]{64}", account) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", origin_oid):
            raise PilotHalted("PILOT_SCOPE_INVALID")
        if number(quantity) != Decimal(1):
            raise PilotHalted("PILOT_ONE_CONTRACT_LIMIT")
        self.scope = {
            "account": account,
            "origin_oid": origin_oid,
            "position_id": identity(position_id),
            "quantity": str(number(quantity)),
            "symbol": "BTC_USDT",
        }
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pilot (id INTEGER PRIMARY KEY CHECK(id=1), scope TEXT NOT NULL, body TEXT NOT NULL)"
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT scope FROM pilot WHERE id=1").fetchone()
            if row is None:
                if existing_journal:
                    raise PilotHalted("EXISTING_JOURNAL_STATE_MISSING_NO_REARM")
                self.db.execute(
                    "INSERT INTO pilot VALUES(1,?,?)",
                    (digest(self.scope), json.dumps({"state": "NEW", "revision": 0, "events": []})),
                )
            elif row[0] != digest(self.scope):
                raise PilotHalted("JOURNAL_SCOPE_MISMATCH")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def _require_recent_entry_fill_for_place(snapshot):
        """Conservative EARLIEST signed fill deadline, not order updateTime alone.

        MEXC order/deal_details/{orderId} documents per-fill timestamp in
        Unix milliseconds. A later order update is NOT proof of a fresh fill.
        No inferred fill time, incomplete volume, duplicated fill or stale
        timestamp can make the request eligible for position-stop review.
        """
        origin = snapshot.get("origin")
        updated = origin.get("updateTime") if type(origin) is dict else None
        if type(updated) is not int or updated <= 0:
            raise PilotHalted("ENTRY_FILL_TIMESTAMP_UNVERIFIED")
        now_ms = int(time.time() * 1000)
        if updated > now_ms + MAX_EXCHANGE_CLOCK_LEAD_MS:
            raise PilotHalted("ENTRY_FILL_CLOCK_SKEW_UNVERIFIED")
        order_id = identity(origin.get("orderId"))
        deals = snapshot.get("origin_deals")
        if type(deals) is not list or not 1 <= len(deals) <= 100:
            raise PilotHalted("ENTRY_FILL_DEALS_UNVERIFIED")
        seen = set()
        total = Decimal(0)
        first_fill_ms = updated
        for deal in deals:
            if type(deal) is not dict:
                raise PilotHalted("ENTRY_FILL_DEAL_SHAPE_UNVERIFIED")
            deal_id = identity(deal.get("id"))
            if deal_id in seen:
                raise PilotHalted("ENTRY_FILL_DUPLICATE_TRADE")
            seen.add(deal_id)
            if (
                identity(deal.get("orderId")) != order_id
                or deal.get("symbol") != "BTC_USDT"
                or type(deal.get("side")) is not int
                or deal["side"] != 3
            ):
                raise PilotHalted("ENTRY_FILL_DEAL_IDENTITY_MISMATCH")
            total += number(deal.get("vol"))
            number(deal.get("price"))
            deal_at = deal.get("timestamp")
            if type(deal_at) is not int or deal_at <= 0:
                raise PilotHalted("ENTRY_FILL_TIMESTAMP_UNVERIFIED")
            if deal_at > now_ms + MAX_EXCHANGE_CLOCK_LEAD_MS:
                raise PilotHalted("ENTRY_FILL_CLOCK_SKEW_UNVERIFIED")
            first_fill_ms = min(first_fill_ms, deal_at)
        if total != Decimal(1):
            raise PilotHalted("ENTRY_FILL_VOLUME_UNVERIFIED")
        now_ms = int(time.time() * 1000)
        if first_fill_ms > now_ms + MAX_EXCHANGE_CLOCK_LEAD_MS:
            raise PilotHalted("ENTRY_FILL_CLOCK_SKEW_UNVERIFIED")
        if now_ms - first_fill_ms > MAX_ENTRY_FILL_AGE_FOR_PLACE_REVIEW_MS:
            raise PilotHalted("ENTRY_FILL_TOO_OLD_FOR_NATIVE_PLACE_REVIEW")
        return first_fill_ms

    def _read(self):
        return json.loads(self.db.execute("SELECT body FROM pilot WHERE id=1").fetchone()[0])

    def diagnostics(self):
        row = self._read()
        return {
            "state": row["state"],
            "revision": row["revision"],
            "live_authorization": False,
            "stop_execution_certified": False,
            "post_transport_available": False,
            "entry_allowed": False,
            "protection_gap_acceptable": False,
            "place_review_deadline_expired": (
                int(time.time() * 1000) > row["place_review_deadline_ms"]
                if type(row.get("place_review_deadline_ms")) is int
                else None
            ),
            "reconciliation_required": row["state"].endswith("UNKNOWN"),
        }

    def _inventory(self, snapshot, *, flat=False):
        if snapshot.get("account") != self.scope["account"]:
            raise PilotHalted("ACCOUNT_MISMATCH")
        observed = snapshot.get("observed_at")
        if type(observed) not in (int, float) or not 0 <= time.time() - observed <= 5:
            raise PilotHalted("SNAPSHOT_STALE")
        positions, stops, counts = (snapshot.get(k) for k in ("positions", "stops", "counts"))
        if (
            type(positions) is not list
            or type(stops) is not list
            or any(type(r) is not dict for r in positions + stops)
        ):
            raise PilotHalted("INVENTORY_SHAPE_INVALID")
        if type(counts) is not dict or any(
            type(counts.get(k)) is not int or counts[k] < 0
            for k in ("sumCount", "stopOrderCount", "limitOrderCount", "planOrderCount", "trackOrderCount")
        ):
            raise PilotHalted("INVENTORY_COUNTS_INVALID")
        if (
            counts["stopOrderCount"] != len(stops)
            or counts["sumCount"] != len(stops)
            or any(counts[k] for k in ("limitOrderCount", "planOrderCount", "trackOrderCount"))
        ):
            raise PilotHalted("UNRELATED_ORDERS_OR_INCOMPLETE_INVENTORY")
        if flat:
            if positions:
                raise PilotHalted("POSITION_NOT_FLAT")
        else:
            if len(positions) != 1:
                raise PilotHalted("POSITION_COUNT_MISMATCH")
            p = positions[0]
            if (
                identity(p.get("positionId")) != self.scope["position_id"]
                or p.get("symbol") != "BTC_USDT"
                or number(p.get("holdVol")) != number(self.scope["quantity"])
            ):
                raise PilotHalted("POSITION_IDENTITY_OR_QUANTITY_MISMATCH")
            if any(
                type(p.get(k)) is not int or p[k] != v
                for k, v in [("positionType", 2), ("openType", 1), ("leverage", 1)]
            ):
                raise PilotHalted("PILOT_REQUIRES_ISOLATED_1X_SHORT")
            o = snapshot.get("origin")
            if (
                type(o) is not dict
                or o.get("externalOid") != self.scope["origin_oid"]
                or o.get("symbol") != "BTC_USDT"
                or identity(o.get("positionId")) != self.scope["position_id"]
            ):
                raise PilotHalted("ENTRY_ORIGIN_UNVERIFIED")
            identity(o.get("orderId"))
            if (
                type(o.get("state")) is not int
                or o["state"] != 3
                or type(o.get("side")) is not int
                or o["side"] != 3
                or number(o.get("dealVol")) != number(self.scope["quantity"])
            ):
                raise PilotHalted("ENTRY_FILL_UNVERIFIED")
        return stops

    def _stop(self, stops, row, price):
        if len(stops) != 1:
            raise PilotHalted("NATIVE_STOP_NOT_UNIQUE")
        stop = stops[0]
        # MEXC documents volType=2 as ENTIRE POSITION; volType=1 is
        # partial-position TP/SL. Require position mode on every subsequent
        # GET, including after an amendment. NEVER silently downgrade to a
        # partial 1-contract stop even if it currently matches holdVol=1.
        if (
            type(stop.get("volType")) is not int
            or stop["volType"] != 2
            or stop.get("profitLossVolType") != "SAME"
        ):
            raise PilotHalted("STOP_QUANTITY_MODE_CHANGED")
        if identity(stop.get("id")) != row["stop_id"] or number(stop.get("stopLossPrice")) != number(price):
            raise PilotHalted("STOP_ID_OR_EXACT_PRICE_MISMATCH")
        audit = assess_native_short_market_stop_row(
            stop=stop,
            symbol="BTC_USDT",
            position_id=self.scope["position_id"],
            position_quantity=number(self.scope["quantity"]),
            maximum_short_stop_price=number(price),
        )
        if not audit.active_native_full_volume_market_sl_observed:
            raise PilotHalted(audit.status)
        # The shared interpreter permits a whole-position TP/SL GET to
        # report zero/omitted vol. An explicit positive reported volume,
        # however, must agree with the owned position size. Reject negative,
        # non-finite, boolean, and oversized values.
        reported = stop.get("vol")
        if reported is not None:
            if type(reported) is bool:
                raise PilotHalted("WHOLE_STOP_REPORTED_VOLUME_INVALID")
            try:
                reported_qty = Decimal(str(reported))
            except (InvalidOperation, TypeError, ValueError):
                raise PilotHalted("WHOLE_STOP_REPORTED_VOLUME_INVALID") from None
            if (
                not reported_qty.is_finite()
                or reported_qty < 0
                or (reported_qty > 0 and reported_qty != number(self.scope["quantity"]))
            ):
                raise PilotHalted("WHOLE_STOP_REPORTED_VOLUME_CONFLICT")
        return stop

    def _require_terminal_stop_contract(self, stop, row):
        """Check canceled stop history preserves native whole-position market SL."""
        if (
            type(stop.get("volType")) is not int
            or stop["volType"] != 2
            or stop.get("profitLossVolType") != "SAME"
            or type(stop.get("stopLossType")) is not int
            or stop["stopLossType"] != 0
            or type(stop.get("stopLossReverse")) is not int
            or stop["stopLossReverse"] != 2
            or type(stop.get("positionType")) is not int
            or stop["positionType"] != 2
            or type(stop.get("lossTrend")) is not int
            or stop["lossTrend"] != 1
            or type(stop.get("errorCode")) is not int
            or stop["errorCode"] != 0
            or number(stop.get("stopLossPrice")) != number(row.get("price"))
        ):
            raise PilotHalted("TERMINAL_STOP_CONTRACT_DRIFT")
        reported = stop.get("vol")
        if reported is not None:
            if type(reported) is bool:
                raise PilotHalted("TERMINAL_STOP_VOLUME_INVALID")
            try:
                qty = Decimal(str(reported))
            except (InvalidOperation, TypeError, ValueError):
                raise PilotHalted("TERMINAL_STOP_VOLUME_INVALID") from None
            if (
                not qty.is_finite()
                or qty < 0
                or (qty > 0 and qty != number(self.scope["quantity"]))
            ):
                raise PilotHalted("TERMINAL_STOP_VOLUME_DRIFT")

    @staticmethod
    def _require_nontriggering_amendment_market(snapshot, price):
        """Review-only market cushion, not a venue guarantee or live permit.

        This checks caller-supplied last-price evidence for the configured
        latest-price trigger. A future production adapter must independently
        acquire, bind and repeat public price/spec observations; this model
        cannot authenticate the supplied market witness.
        """
        witness = snapshot.get("market")
        if type(witness) is not dict or witness.get("symbol") != "BTC_USDT":
            raise PilotHalted("AMEND_MARKET_WITNESS_MISSING")
        at_ms = witness.get("observed_at_ms")
        now_ms = int(time.time() * 1000)
        if (
            type(at_ms) is not int
            or not 0 <= now_ms - at_ms <= 2000
        ):
            raise PilotHalted("AMEND_MARKET_WITNESS_STALE_OR_FUTURE")
        last_price = number(witness.get("lastPrice"))
        tick = number(witness.get("tickSize"))
        new_price = number(price)
        if (new_price / tick) != (new_price / tick).to_integral_value():
            raise PilotHalted("AMEND_STOP_NOT_ON_TICK")
        # Conservative review-only threshold. 50 bps OR 5 ticks,
        # whichever is larger. Neither threshold is a venue execution
        # guarantee, and this MUST NOT be promoted to live permission.
        minimum_distance = max(last_price * Decimal("0.005"), tick * 5)
        if new_price <= last_price + minimum_distance:
            raise PilotHalted("AMEND_STOP_TOO_CLOSE_TO_LATEST_TRIGGER")

    def proposal(self, action, snapshot, *, price=None):
        row = self._read()
        stops = self._inventory(snapshot, flat=action == "CANCEL")
        if action == "PLACE":
            if row["state"] != "NEW" or stops:
                raise PilotHalted("PLACE_REQUIRES_EMPTY_STOP_INVENTORY")
            self._require_recent_entry_fill_for_place(snapshot)
            payload = {
                "positionId": self.scope["position_id"],
                "vol": self.scope["quantity"],
                "stopLossPrice": str(number(price)),
                "lossTrend": 1,
                "profitTrend": 1,
                "volType": 2,
                "profitLossVolType": "SAME",
                "stopLossReverse": 2,
                "stopLossType": 0,
            }
            path = "/api/v1/private/stoporder/place"
        elif action == "AMEND":
            if row["state"] != "PROTECTED_OBSERVED":
                raise PilotHalted("AMEND_REQUIRES_VERIFIED_STOP")
            self._stop(stops, row, row["price"])
            if number(price) >= number(row["price"]):
                raise PilotHalted("STOP_NOT_TIGHTER")
            self._require_nontriggering_amendment_market(snapshot, price)
            payload = {
                "stopPlanOrderId": row["stop_id"],
                "stopLossPrice": str(number(price)),
                "lossTrend": 1,
                "stopLossReverse": 2,
            }
            path = "/api/v1/private/stoporder/change_plan_price"
        elif action == "CLOSE":
            if row["state"] not in ("NEW", "PROTECTED_OBSERVED", "PLACE_UNKNOWN", "AMEND_UNKNOWN"):
                raise PilotHalted("CLOSE_STATE_INVALID")
            # No cancel-first window. Existing stop remains until GET proves flat.
            payload = {
                "symbol": "BTC_USDT",
                "positionId": self.scope["position_id"],
                "vol": self.scope["quantity"],
                "side": 2,
                "sideType": "buy",
                "positionType": "short",
                "positionMode": 1,
                "openType": 1,
                "type": 5,
                "externalOid": "psp-" + digest([self.scope, row["revision"]])[:24],
            }
            path = "/api/v1/private/order/create"
        elif action == "CANCEL":
            if (
                row["state"] != "FLAT_OBSERVED"
                or len(stops) != 1
                or identity(stops[0].get("id")) != row.get("stop_id")
                or identity(stops[0].get("positionId")) != self.scope["position_id"]
                or stops[0].get("symbol") != self.scope["symbol"]
                or type(stops[0].get("orderId")) not in (int, str)
                or str(stops[0]["orderId"]) != "0"
                or any(
                    type(stops[0].get(k)) is not int or stops[0][k] != v
                    for k, v in (("state", 1), ("isFinished", 0), ("triggerSide", 0))
                )
            ):
                raise PilotHalted("CANCEL_REQUIRES_FLAT_OWNED_STOP")
            self._require_terminal_stop_contract(stops[0], row)
            payload = [{"stopPlanOrderId": row["stop_id"]}]
            path = "/api/v1/private/stoporder/cancel"
        else:
            raise PilotHalted("ACTION_INVALID")
        return {
            "action": action,
            "path": path,
            "payload": payload,
            "revision": row["revision"],
            "account": self.scope["account"],
        }

    def stage(self, request, snapshot, approval):
        """Durably latch UNKNOWN before request could leave process. No dispatch."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            price = request["payload"].get("stopLossPrice") if type(request.get("payload")) is dict else None
            expected = self.proposal(request["action"], snapshot, price=price)
            if (
                expected != request
                or type(approval) is not Approval
                or approval.account != self.scope["account"]
                or approval.request_sha256 != digest(expected)
                or approval.phrase != "REVIEW_ONLY_POSITION_STOP_PILOT"
                or not 0 < approval.expires_at - time.time() <= 60
            ):
                raise PilotHalted("SCOPED_OPERATOR_REVIEW_REQUIRED")
            row = self._read()
            if request["action"] == "PLACE":
                # Diagnostic evidence only. Does NOT authorize a real entry,
                # and the eventual venue POST/GET may exceed this deadline.
                recorded_fill_ms = self._require_recent_entry_fill_for_place(snapshot)
                row["entry_first_fill_ms"] = recorded_fill_ms
                row["place_review_deadline_ms"] = (
                    recorded_fill_ms + MAX_ENTRY_FILL_AGE_FOR_PLACE_REVIEW_MS
                )
            if row.get("pending"):
                row["events"].append({"kind": "SUPERSEDED_PENDING", "request": row["pending"]})
            row.update(state=request["action"] + "_UNKNOWN", pending=request)
            row["revision"] += 1
            row["events"].append(
                {
                    "kind": "STAGED_UNKNOWN",
                    "request": request,
                    "request_sha256": digest(request),
                    "at": time.time(),
                }
            )
            self.db.execute("UPDATE pilot SET body=? WHERE id=1", (json.dumps(row),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def record_post_outcome(self, request_sha256, response=None):
        """Journal supplied/mock responses without dispatch, raw data or retry.

        Rejection and acknowledgement both require subsequent GET reconciliation.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._read()
            if not row["state"].endswith("UNKNOWN") or digest(row["pending"]) != request_sha256:
                raise PilotHalted("OUTCOME_ATTEMPT_MISMATCH")
            kind = "AMBIGUOUS"
            if type(response) is dict and type(response.get("code")) is int:
                if response.get("success") is False and response.get("data") is None:
                    kind = "REJECTED_REQUIRES_GET"
                elif response.get("success") is True and response["code"] == 0:
                    kind = "ACK_REQUIRES_GET"
                    if row["state"] == "PLACE_UNKNOWN":
                        try:
                            acknowledged_id = identity(response.get("data"))
                        except PilotHalted:
                            kind = "AMBIGUOUS"
                        else:
                            prior = row.get("acknowledged_stop_id")
                            if prior is not None and prior != acknowledged_id:
                                row["placement_identity_conflict"] = True
                                kind = "CONFLICTING_PLACEMENT_IDENTITY"
                            else:
                                row["acknowledged_stop_id"] = acknowledged_id
                    elif row["state"] == "CLOSE_UNKNOWN":
                        # MEXC order/create documents data={orderId, ts}.
                        # Persist only the validated order ID, never a body.
                        # A contradictory acknowledgement latches permanently.
                        data = response.get("data")
                        try:
                            if type(data) is not dict:
                                raise PilotHalted("CLOSE_ACK_SHAPE_UNVERIFIED")
                            acknowledged_id = identity(data.get("orderId"))
                        except PilotHalted:
                            kind = "AMBIGUOUS"
                        else:
                            prior = row.get("acknowledged_close_order_id")
                            if prior is not None and prior != acknowledged_id:
                                row["close_identity_conflict"] = True
                                kind = "CONFLICTING_CLOSE_IDENTITY"
                            else:
                                row["acknowledged_close_order_id"] = acknowledged_id
            row["events"].append({"kind": kind, "request_sha256": request_sha256, "at": time.time()})
            row["revision"] += 1
            self.db.execute("UPDATE pilot SET body=? WHERE id=1", (json.dumps(row),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _verify_close_deals(self, deals, order_id):
        """Independent fill-detail witness; close order.state/dealVol alone is insufficient.

        MEXC /private/order/deal_details/{orderId} returns one trade per row.
        Never treat absent, duplicated, wrong-order or partial rows as closure.
        """
        if type(deals) is not list or not 1 <= len(deals) <= 100:
            raise PilotHalted("CLOSE_DEAL_HISTORY_UNVERIFIED")
        seen = set()
        filled = Decimal(0)
        for deal in deals:
            if type(deal) is not dict:
                raise PilotHalted("CLOSE_DEAL_ROW_UNVERIFIED")
            deal_id = identity(deal.get("id"))
            if deal_id in seen:
                raise PilotHalted("CLOSE_DEAL_DUPLICATE")
            seen.add(deal_id)
            if (
                identity(deal.get("orderId")) != order_id
                or deal.get("symbol") != self.scope["symbol"]
                or type(deal.get("side")) is not int
                or deal["side"] != 2
            ):
                raise PilotHalted("CLOSE_DEAL_IDENTITY_MISMATCH")
            filled += number(deal.get("vol"))
            number(deal.get("price"))
        if filled != number(self.scope["quantity"]):
            raise PilotHalted("CLOSE_DEAL_VOLUME_MISMATCH")

    def observe(
        self, snapshot, *, stop_id=None, close_order=None, close_deals=None,
        canceled_stop=None, expected_revision=None
    ):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._read()
            if expected_revision is not None and row["revision"] != expected_revision:
                raise PilotHalted("JOURNAL_CHANGED_DURING_GET")
            action = row["state"]
            if action in ("PLACE_UNKNOWN", "AMEND_UNKNOWN"):
                stops = self._inventory(snapshot)
                if action == "PLACE_UNKNOWN":
                    # Even a perfectly shaped stop observed too late cannot
                    # certify that this experiment met its review window.
                    # Preserve UNKNOWN and allow supervised emergency CLOSE.
                    deadline_ms = row.get("place_review_deadline_ms")
                    if (
                        type(deadline_ms) is not int
                        or int(time.time() * 1000) > deadline_ms
                    ):
                        raise PilotHalted("NATIVE_STOP_PROTECTION_GAP_DEADLINE_EXCEEDED")
                    if row.get("placement_identity_conflict"):
                        raise PilotHalted("PLACEMENT_IDENTITY_CONFLICT_REQUIRES_INVESTIGATION")
                    acknowledged_id = row.get("acknowledged_stop_id")
                    if acknowledged_id is not None:
                        if stop_id is not None and identity(stop_id) != acknowledged_id:
                            raise PilotHalted("PLACEMENT_ACK_GET_IDENTITY_MISMATCH")
                        stop_id = acknowledged_id
                    row["stop_id"] = identity(stop_id)
                price = row["pending"]["payload"]["stopLossPrice"]
                self._stop(stops, row, price)
                row.update(state="PROTECTED_OBSERVED", price=price)
            elif action == "CLOSE_UNKNOWN":
                self._inventory(snapshot, flat=True)
                if row.get("close_identity_conflict"):
                    raise PilotHalted("CLOSE_ACK_IDENTITY_CONFLICT_REQUIRES_INVESTIGATION")
                o = close_order
                if (
                    type(o) is not dict
                    or o.get("externalOid") != row["pending"]["payload"]["externalOid"]
                    or o.get("symbol") != "BTC_USDT"
                    or identity(o.get("positionId")) != self.scope["position_id"]
                    or type(o.get("state")) is not int
                    or o["state"] != 3
                    or type(o.get("side")) is not int
                    or o["side"] != 2
                    or number(o.get("dealVol")) != number(self.scope["quantity"])
                ):
                    raise PilotHalted("CLOSE_FILL_NOT_RECONCILED")
                close_id = identity(o.get("orderId"))
                acknowledged_id = row.get("acknowledged_close_order_id")
                if acknowledged_id is not None and close_id != acknowledged_id:
                    raise PilotHalted("CLOSE_ACK_GET_IDENTITY_MISMATCH")
                self._verify_close_deals(close_deals, close_id)
                row["state"] = "FLAT_OBSERVED"
            elif action in ("CANCEL_UNKNOWN", "FLAT_OBSERVED"):
                stops = self._inventory(snapshot, flat=True)
                s = canceled_stop
                if (
                    stops
                    or type(s) is not dict
                    or identity(s.get("id")) != row.get("stop_id")
                    or identity(s.get("positionId")) != self.scope["position_id"]
                    or s.get("symbol") != "BTC_USDT"
                    or type(s.get("orderId")) not in (int, str)
                    or str(s["orderId"]) != "0"
                    or any(
                        type(s.get(k)) is not int or s[k] != v
                        for k, v in [("state", 2), ("isFinished", 1), ("triggerSide", 0)]
                    )
                ):
                    raise PilotHalted("CANCEL_TERMINAL_NOT_RECONCILED")
                self._require_terminal_stop_contract(s, row)
                row["state"] = "FLAT_STOP_CANCELED_OBSERVED"
            else:
                raise PilotHalted("NO_PENDING_RECONCILIATION")
            row["revision"] += 1
            row["events"].append({"kind": "GET_OBSERVED", "state": row["state"]})
            self.db.execute("UPDATE pilot SET body=? WHERE id=1", (json.dumps(row),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    async def observe_get(self, probe, *, stop_id=None):
        """Signed GET-only reconciliation, with matching inventory bookends.

        History scan is bounded and requires a unique exact known stop row.
        It never certifies global history completeness or market execution.
        """
        if type(probe) is not ReadOnlyMexcProbe:
            raise PilotHalted("SIGNED_GET_PROBE_REQUIRED")
        row = self._read()
        try:
            first = await collect_get_snapshot(probe, self.scope["account"], self.scope["origin_oid"])
            close_order = None
            close_deals = None
            terminal = None
            if row["state"] == "CLOSE_UNKNOWN":
                close_oid = row["pending"]["payload"]["externalOid"]
                close_order = await probe.raw_order_by_external_oid(
                    "BTC_USDT", close_oid
                )
                if type(close_order) is not dict:
                    raise PilotHalted("CLOSE_GET_ORDER_UNVERIFIED")
                close_id = identity(close_order.get("orderId"))
                close_deals = await probe.raw_order_deal_details(close_id)
            async def read_owned_terminal_stop():
                """Repeat the bounded finished-stop scan; no single transient row suffices."""
                matches = []
                seen = set()
                for page in range(1, 11):
                    rows = await probe.raw_stop_page(page, 100)
                    for item in rows:
                        key = identity(item.get("id"))
                        if key in seen:
                            raise PilotHalted("STOP_HISTORY_DUPLICATE")
                        seen.add(key)
                        if key == row.get("stop_id"):
                            matches.append(item)
                    if len(rows) < 100:
                        break
                else:
                    raise PilotHalted("STOP_HISTORY_SCAN_BOUND")
                if len(matches) != 1:
                    raise PilotHalted("STOP_HISTORY_IDENTITY_UNVERIFIED")
                return matches[0]

            if row["state"] in ("CANCEL_UNKNOWN", "FLAT_OBSERVED"):
                terminal = await read_owned_terminal_stop()
            last = await collect_get_snapshot(probe, self.scope["account"], self.scope["origin_oid"])
            if any(
                first[k] != last[k]
                for k in ("positions", "stops", "counts", "origin", "origin_deals")
            ):
                raise PilotHalted("RECONCILIATION_INVENTORY_CHANGED")
            if row["state"] == "CLOSE_UNKNOWN":
                # Independent second signed GET: both the order and its
                # immutable trade ledger must remain stable across bookends.
                second_order = await probe.raw_order_by_external_oid("BTC_USDT", close_oid)
                second_deals = await probe.raw_order_deal_details(close_id)
                if close_order != second_order or close_deals != second_deals:
                    raise PilotHalted("CLOSE_ORDER_OR_DEALS_DRIFT")
            if row["state"] in ("CANCEL_UNKNOWN", "FLAT_OBSERVED"):
                # An unrelated inventory can stay flat while terminal stop
                # history changes. Never certify cancellation on one read.
                if terminal != await read_owned_terminal_stop():
                    raise PilotHalted("STOP_TERMINAL_HISTORY_DRIFT")
            last["observed_at"] = first["observed_at"]
            if self._read()["revision"] != row["revision"]:
                raise PilotHalted("JOURNAL_CHANGED_DURING_GET")
            self.observe(
                last,
                stop_id=stop_id,
                close_order=close_order,
                close_deals=close_deals,
                canceled_stop=terminal,
                expected_revision=row["revision"],
            )
        except Exception:  # noqa: BLE001 - no private transport data in exceptions
            raise PilotHalted("SIGNED_GET_RECONCILIATION_UNVERIFIED") from None

    def dispatch(self, *args, **kwargs):
        raise PilotHalted("LIVE_PILOT_TRANSPORT_NOT_IMPLEMENTED_OR_AUTHORIZED")
