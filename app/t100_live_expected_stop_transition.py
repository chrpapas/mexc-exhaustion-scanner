"""Fail-closed recognition of journaled native stop amendments.

Pure reconciliation only. This module never sends, edits, or cancels an
exchange order. It is intended to distinguish a stop transition that matches
a previously durable trader intent from unexplained exchange drift.

A verified expected transition is NOT proof of account-wide completeness or
exchange atomicity and never authorizes live trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ExpectedStopTransition:
    external_id: str
    symbol: str
    position_id: str
    previous_stop_order_id: str
    previous_stop: Decimal
    proposed_stop: Decimal

    @classmethod
    def build(
        cls,
        *,
        external_id: str,
        symbol: str,
        position_id: str,
        previous_stop_order_id: str,
        previous_stop: Decimal,
        proposed_stop: Decimal,
    ) -> "ExpectedStopTransition":
        if not all(
            isinstance(value, str) and value
            for value in (
                external_id,
                symbol,
                position_id,
                previous_stop_order_id,
            )
        ):
            raise ValueError("EXPECTED_STOP_IDENTITY_MISSING")
        for value in (previous_stop, proposed_stop):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError("EXPECTED_STOP_PRICE_INVALID")
        if proposed_stop >= previous_stop:
            raise ValueError("EXPECTED_SHORT_STOP_MUST_TIGHTEN")
        return cls(
            external_id=external_id,
            symbol=symbol,
            position_id=position_id,
            previous_stop_order_id=previous_stop_order_id,
            previous_stop=previous_stop,
            proposed_stop=proposed_stop,
        )


def _integer(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if str(value) == str(raw) else None


def _positive_decimal(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _stop_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("stopPlanOrderId") or "")


def _target_rows(
    rows: list[dict[str, Any]],
    expected: ExpectedStopTransition,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("positionId") or "") == expected.position_id
        and row.get("symbol") == expected.symbol
    ]


def assess_expected_stop_transition(
    *,
    before_stops: Any,
    after_stops: Any,
    expected: ExpectedStopTransition,
    finished_stops: Any | None = None,
) -> dict[str, Any]:
    """Classify one observed stop transition against one durable intent.

    Same-ID in-place tightening can verify directly. If MEXC replaces the
    native stop identity, the old stop must also be observed in terminal
    history as CANCELED, untriggered and finished before the replacement is
    considered expected.

    A protection gap is never accepted as an expected amendment.
    """
    result: dict[str, Any] = {
        "expected_transition_verified": False,
        "same_stop_identity": False,
        "replacement_stop_identity": False,
        "old_stop_terminal_cancel_verified": False,
        "before_stop_active": False,
        "after_stop_active": False,
        "before_price_matches_journal": False,
        "after_price_tightened_to_expected_level": False,
        "protection_gap_observed": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }

    if not isinstance(expected, ExpectedStopTransition):
        result["status"] = "INVALID_EXPECTED_TRANSITION"
        return result
    if (
        not isinstance(before_stops, list)
        or not isinstance(after_stops, list)
        or any(not isinstance(row, dict) for row in before_stops + after_stops)
    ):
        result["status"] = "INVALID_CURRENT_STOP_EVIDENCE"
        return result
    if finished_stops is not None and (
        not isinstance(finished_stops, list)
        or any(not isinstance(row, dict) for row in finished_stops)
    ):
        result["status"] = "INVALID_FINISHED_STOP_EVIDENCE"
        return result

    before = _target_rows(before_stops, expected)
    after = _target_rows(after_stops, expected)

    result["before_matching_rows"] = len(before)
    result["after_matching_rows"] = len(after)

    if len(before) != 1:
        result["status"] = "BEFORE_STOP_NOT_UNIQUE"
        return result
    if len(after) == 0:
        result["protection_gap_observed"] = True
        result["status"] = "PROTECTION_GAP_NOT_ACCEPTED"
        return result
    if len(after) != 1:
        result["status"] = "AFTER_STOP_NOT_UNIQUE"
        return result

    before_row = before[0]
    after_row = after[0]

    before_id = _stop_id(before_row)
    after_id = _stop_id(after_row)
    if not before_id or not after_id:
        result["status"] = "STOP_IDENTITY_MISSING"
        return result
    if before_id != expected.previous_stop_order_id:
        result["status"] = "JOURNAL_PREVIOUS_STOP_ID_MISMATCH"
        return result

    before_active = (
        _integer(before_row.get("state")) == 1
        and (
            "isFinished" not in before_row
            or _integer(before_row.get("isFinished")) == 0
        )
    )
    after_active = (
        _integer(after_row.get("state")) == 1
        and (
            "isFinished" not in after_row
            or _integer(after_row.get("isFinished")) == 0
        )
    )
    result["before_stop_active"] = before_active
    result["after_stop_active"] = after_active
    if not before_active or not after_active:
        result["status"] = "ACTIVE_STOP_STATE_INVALID"
        return result

    before_price = _positive_decimal(before_row.get("stopLossPrice"))
    after_price = _positive_decimal(after_row.get("stopLossPrice"))
    if before_price is None or after_price is None:
        result["status"] = "STOP_PRICE_INVALID"
        return result

    result["before_price_matches_journal"] = (
        before_price == expected.previous_stop
    )
    result["after_price_tightened_to_expected_level"] = (
        after_price <= expected.proposed_stop
        and after_price < before_price
    )
    if not result["before_price_matches_journal"]:
        result["status"] = "JOURNAL_PREVIOUS_STOP_PRICE_MISMATCH"
        return result
    if not result["after_price_tightened_to_expected_level"]:
        result["status"] = "AFTER_STOP_NOT_EXPECTED_TIGHTENING"
        return result

    if after_id == before_id:
        result["same_stop_identity"] = True
        result["expected_transition_verified"] = True
        result["status"] = "EXPECTED_STOP_AMENDMENT_IN_PLACE_VERIFIED"
        return result

    result["replacement_stop_identity"] = True
    if finished_stops is None:
        result["status"] = "STOP_REPLACEMENT_TERMINAL_PROVENANCE_REQUIRED"
        return result

    terminal = [
        row
        for row in finished_stops
        if str(row.get("positionId") or "") == expected.position_id
        and row.get("symbol") == expected.symbol
        and _stop_id(row) == expected.previous_stop_order_id
    ]
    if len(terminal) != 1:
        result["status"] = "OLD_STOP_TERMINAL_ROW_NOT_UNIQUE"
        return result

    old = terminal[0]
    terminal_cancel = (
        _integer(old.get("state")) == 2
        and _integer(old.get("triggerSide")) == 0
        and _integer(old.get("isFinished")) == 1
    )
    result["old_stop_terminal_cancel_verified"] = terminal_cancel
    if not terminal_cancel:
        result["status"] = "OLD_STOP_TERMINAL_CANCEL_UNVERIFIED"
        return result

    result["expected_transition_verified"] = True
    result["status"] = "EXPECTED_STOP_AMENDMENT_REPLACEMENT_VERIFIED"
    return result
