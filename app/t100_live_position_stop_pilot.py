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
        after = await inventory()
        if before != after:
            raise PilotHalted("INVENTORY_CHANGED")
        return dict(after, origin=origin, account=account, observed_at=started)
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
        # This experiment requested fixed quantity/SAME semantics. Do not
        # silently accept a different volume mode after placement or amendment,
        # even if the generic audit currently computes equal coverage.
        if (
            type(stop.get("volType")) is not int
            or stop["volType"] != 1
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
        if number(stop.get("vol")) != number(self.scope["quantity"]):
            raise PilotHalted("EXPLICIT_QUANTITY_REQUIRED")
        return stop

    def proposal(self, action, snapshot, *, price=None):
        row = self._read()
        stops = self._inventory(snapshot, flat=action == "CANCEL")
        if action == "PLACE":
            if row["state"] != "NEW" or stops:
                raise PilotHalted("PLACE_REQUIRES_EMPTY_STOP_INVENTORY")
            payload = {
                "positionId": self.scope["position_id"],
                "vol": self.scope["quantity"],
                "stopLossPrice": str(number(price)),
                "lossTrend": 1,
                "profitTrend": 1,
                "volType": 1,
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
            if row["state"] in ("CANCEL_UNKNOWN", "FLAT_OBSERVED"):
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
                terminal = matches[0]
            last = await collect_get_snapshot(probe, self.scope["account"], self.scope["origin_oid"])
            if any(first[k] != last[k] for k in ("positions", "stops", "counts", "origin")):
                raise PilotHalted("RECONCILIATION_INVENTORY_CHANGED")
            if row["state"] == "CLOSE_UNKNOWN":
                # Independent second signed GET: both the order and its
                # immutable trade ledger must remain stable across bookends.
                second_order = await probe.raw_order_by_external_oid("BTC_USDT", close_oid)
                second_deals = await probe.raw_order_deal_details(close_id)
                if close_order != second_order or close_deals != second_deals:
                    raise PilotHalted("CLOSE_ORDER_OR_DEALS_DRIFT")
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
