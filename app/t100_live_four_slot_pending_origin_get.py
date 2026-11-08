"""Explicit GET-only diagnostic for unresolved V40 order origins after restart.

This is intentionally SEPARATE from normal restart preflight: pending
SUBMITTING/UNKNOWN/ACK/HALTED attempts block that path. This observer
uses their exact immutable symbols + externalOids for at most two private
GET rounds. An order absent twice may STILL have been POSTed. An order
present twice does NOT prove its fill, native stop, collateral or ownership.

No request is a POST, no DB writes, no exchange retries, no account scans,
no manual-position adoption, no trade permission and no sensitive output.
This module never creates clients or reads credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol

from app.t100_live_four_slot_restart_supervisor import (
    _triage_contract_verified,
)
from app.t100_live_four_slot_restart_triage import (
    FourSlotRestartTriage, RecoveryAttempt,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class PrivateGetOnly(Protocol):
    async def _private_get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class PendingOriginGetReport:
    status: str
    origins_checked: int = 0
    origin_orders_seen: int = 0
    origins_absent_unknown: int = 0
    reserved_unstaged: int = 0
    order_fill_verified: bool = False
    native_stop_protection_verified: bool = False
    account_inventory_verified: bool = False
    retry_allowed: bool = False
    manual_position_adopted: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    database_writes_sent: bool = False
    read_only: bool = True

    def safe_summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "origins_checked": self.origins_checked,
            "origin_orders_seen": self.origin_orders_seen,
            "origins_absent_unknown": self.origins_absent_unknown,
            "reserved_unstaged": self.reserved_unstaged,
            "order_fill_verified": False,
            "native_stop_protection_verified": False,
            "account_inventory_verified": False,
            "retry_allowed": False,
            "manual_position_adopted": False,
            "entry_allowed": False,
            "live_authorization": False,
            "mexc_orders_sent": False,
            "database_writes_sent": False,
            "read_only": True,
        }


async def observe_pending_origins_get_only(
    *, triage: FourSlotRestartTriage, client: PrivateGetOnly,
) -> PendingOriginGetReport:
    """No venue request until ALL supplied durable origins validate."""
    def halt(code: str) -> PendingOriginGetReport:
        return PendingOriginGetReport(status=code)

    # Classify malformed order origins before the stricter general triage
    # contract. Both errors block *all* GETs, but the established origin-
    # specific diagnostic must remain stable for operator reconciliation.
    if (
        type(triage) is FourSlotRestartTriage
        and type(triage.attempts) is tuple
        and all(type(a) is RecoveryAttempt for a in triage.attempts)
        and any(
            type(a.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", a.symbol) is None
            or type(a.external_oid) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", a.external_oid) is None
            for a in triage.attempts
        )
    ):
        return halt("PENDING_ORIGIN_IDENTITIES_INVALID_NO_RETRY")

    if (
        not _triage_contract_verified(triage)
        or triage.status != "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY"
        or triage.pending_attempts < 1
        or client is None
        or not callable(getattr(client, "_private_get", None))
    ):
        return halt("PENDING_ORIGIN_TRIAGE_OR_CLIENT_INVALID_NO_RETRY")

    pending = [
        a for a in triage.attempts if a.slot_state != "PROTECTED"
    ]
    staged = [a for a in pending if a.slot_state != "RESERVED"]
    reserved = len(pending) - len(staged)
    # Validate all before ANY network request. PREPARED-like RESERVED
    # slots have no durable staged intent and are not GET-queried.
    if (
        any(
            type(a.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", a.symbol) is None
            or type(a.external_oid) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", a.external_oid) is None
            or a.slot_state not in (
                "RESERVED", "SUBMITTING", "UNKNOWN", "ACK", "HALTED",
            )
            for a in pending
        )
    ):
        return halt("PENDING_ORIGIN_IDENTITIES_INVALID_NO_RETRY")
    if not staged:
        return PendingOriginGetReport(
            status="PENDING_ORIGIN_RESERVED_ONLY_NO_EXCHANGE_INFERENCE",
            reserved_unstaged=reserved,
        )

    probe = ReadOnlyMexcProbe(client)
    rounds: list[list[dict[str, Any] | None]] = []
    try:
        for _ in range(2):
            rows = []
            for attempt in staged:
                row = await probe.raw_order_by_external_oid(
                    attempt.symbol, attempt.external_oid,
                )
                # The transport validates orderId / exact origin, but
                # order SIDE still must be the original SHORT_SELL.
                if row is not None and (
                    type(row.get("side")) is not int
                    or row["side"] != 3
                ):
                    return halt("PENDING_ORIGIN_ORDER_SIDE_UNVERIFIED_NO_RETRY")
                rows.append(row)
            rounds.append(rows)
    except Exception:
        # No raw host, URL, credential, symbol or exchange body in result.
        return halt("PENDING_ORIGIN_GET_FAILED_NO_RETRY")

    # Double-read consistency detects observed changes, not ABA or
    # exchange/database distributed atomicity. No fill/stop inference.
    from app.t100_live_four_slot_snapshot_coordinator import _hash_rows
    try:
        fingerprint = [
            _hash_rows({
                "order": ([] if row is None else [row]),
            })
            for round_ in rounds for row in round_
        ]
    except Exception:
        return halt("PENDING_ORIGIN_ORDER_SHAPE_UNVERIFIED_NO_RETRY")

    checked = len(staged)
    if any(
        fingerprint[i] != fingerprint[i + checked]
        for i in range(checked)
    ):
        return PendingOriginGetReport(
            status="PENDING_ORIGIN_ORDER_BOOKENDS_CHANGED_NO_RETRY",
            origins_checked=checked, reserved_unstaged=reserved,
        )

    seen = sum(row is not None for row in rounds[1])
    absent = checked - seen
    if seen == checked:
        status = "PENDING_ORIGIN_ORDERS_SEEN_UNPROTECTED_NONAUTHORIZING"
    elif absent == checked:
        status = "PENDING_ORIGIN_ALL_ABSENT_STILL_UNKNOWN_NO_RETRY"
    else:
        status = "PENDING_ORIGIN_MIXED_OBSERVATIONS_NO_RETRY"
    return PendingOriginGetReport(
        status=status, origins_checked=checked,
        origin_orders_seen=seen, origins_absent_unknown=absent,
        reserved_unstaged=reserved,
    )
