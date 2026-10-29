"""Immutable close provenance from an executed MEXC native TP/SL row.

MEXC documents placeOrderId on a finished TP/SL row as the order ID created
after successful placement. For T100 short positions, a normal stop close is
accepted only when the exact expected native stop is finished/executed,
triggered on STOP_LOSS, has no exchange error, and reports the full actual
placed quantity. The resulting placeOrderId can then be reconciled through
the order-scoped fill endpoint.

Historical TP/SL pagination completeness is NOT inferred. The bounded finder
provides positive evidence only when the exact immutable stop ID is found.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class CloseProvenanceHalted(RuntimeError):
    pass


def _integer(raw: Any) -> int:
    if isinstance(raw, bool):
        raise CloseProvenanceHalted("INVALID_CLOSE_INTEGER")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise CloseProvenanceHalted("INVALID_CLOSE_INTEGER") from exc
    if str(value) != str(raw):
        raise CloseProvenanceHalted("INVALID_CLOSE_INTEGER")
    return value


def _positive(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CloseProvenanceHalted("INVALID_CLOSE_NUMERIC") from exc
    if not value.is_finite() or value <= 0:
        raise CloseProvenanceHalted("INVALID_CLOSE_NUMERIC")
    return value


@dataclass(frozen=True)
class VerifiedStopCloseProvenance:
    stop_order_id: str
    position_id: str
    symbol: str
    close_order_id: str
    contracts: Decimal
    trigger_side: str = "STOP_LOSS"
    live_authorization: bool = False


def build_verified_stop_close_provenance(
    *,
    finished_stop: Any,
    expected_stop_order_id: str,
    expected_position_id: str,
    expected_symbol: str,
    expected_contracts: Decimal,
) -> VerifiedStopCloseProvenance:
    if (
        not isinstance(finished_stop, dict)
        or not isinstance(expected_stop_order_id, str)
        or not expected_stop_order_id
        or not isinstance(expected_position_id, str)
        or not expected_position_id
        or not isinstance(expected_symbol, str)
        or not expected_symbol
        or not isinstance(expected_contracts, Decimal)
        or not expected_contracts.is_finite()
        or expected_contracts <= 0
    ):
        raise CloseProvenanceHalted("CLOSE_PROVENANCE_INPUT_INVALID")

    stop_id = str(
        finished_stop.get("id")
        or finished_stop.get("stopPlanOrderId")
        or ""
    )
    position_id = str(finished_stop.get("positionId") or "")
    symbol = finished_stop.get("symbol")
    close_order_id = str(finished_stop.get("placeOrderId") or "")
    if (
        stop_id != expected_stop_order_id
        or position_id != expected_position_id
        or symbol != expected_symbol
        or not close_order_id.isdigit()
        or int(close_order_id) <= 0
    ):
        raise CloseProvenanceHalted("CLOSE_PROVENANCE_IDENTITY_MISMATCH")

    if (
        _integer(finished_stop.get("state")) != 3
        or _integer(finished_stop.get("triggerSide")) != 2
        or _integer(finished_stop.get("positionType")) != 2
        or _integer(finished_stop.get("isFinished")) != 1
        or _integer(finished_stop.get("errorCode")) != 0
    ):
        raise CloseProvenanceHalted("CLOSE_PROVENANCE_TERMINAL_STATE_INVALID")

    actual = _positive(finished_stop.get("realityVol"))
    if actual != expected_contracts:
        raise CloseProvenanceHalted("CLOSE_PROVENANCE_VOLUME_MISMATCH")

    return VerifiedStopCloseProvenance(
        stop_order_id=stop_id,
        position_id=position_id,
        symbol=symbol,
        close_order_id=close_order_id,
        contracts=actual,
    )


async def find_finished_stop_by_id(
    probe: ReadOnlyMexcProbe,
    *,
    stop_order_id: str,
    page_size: int = 100,
    max_pages: int = 20,
) -> dict[str, Any]:
    """Find exact finished stop ID; never interprets not-found as completeness."""
    if (
        not isinstance(stop_order_id, str)
        or not stop_order_id.isdigit()
        or int(stop_order_id) <= 0
        or type(page_size) is not int
        or not 1 <= page_size <= 100
        or type(max_pages) is not int
        or not 1 <= max_pages <= 20
    ):
        raise CloseProvenanceHalted("INVALID_FINISHED_STOP_LOOKUP_ARGUMENT")

    for page in range(1, max_pages + 1):
        try:
            rows = await probe.raw_stop_page(
                page,
                page_size,
                is_finished=1,
            )
        except Exception as exc:
            raise CloseProvenanceHalted(
                "FINISHED_STOP_LOOKUP_READ_FAILED"
            ) from exc
        matches = [
            row for row in rows
            if str(row.get("id") or row.get("stopPlanOrderId") or "")
            == stop_order_id
        ]
        if len(matches) > 1:
            raise CloseProvenanceHalted(
                "FINISHED_STOP_ID_DUPLICATE_IN_PAGE"
            )
        if len(matches) == 1:
            return matches[0]

    raise CloseProvenanceHalted(
        "TARGET_FINISHED_STOP_NOT_FOUND_WITHIN_BOUNDED_SEARCH"
    )
