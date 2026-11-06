"""Read-only independent finality proof for ONE durable T100-owned position.

MEXC historical position records provide terminal state and close quantity;
the open-position endpoint also supports an exact positionId filter. Neither
alone is sufficient. We require two independent, matching POSITIVE historical
position records and two exact-position GET reads, all consistent. The
observed bare-array API response is never an exhaustive history census.
Metadata-backed responses continue to require full declared pagination.

This proves only the specified managed position's closure. Manual/foreign
positions may still be open; this is NOT account-wide flatness, admission to
new trading, exchange atomicity, or an emergency-close POST authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_owned_emergency_guardian import OwnedShort, _validate_owned
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class OwnedPositionFinalityHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedPositionFinality:
    status: str
    closed_position_verified: bool = False
    historical_pages_exhausted: bool = False
    open_position_absent_twice: bool = False
    account_flat_certified: bool = False
    live_authorization: bool = False
    emergency_close_authorized: bool = False


def _decimal0(raw: Any) -> Decimal | None:
    if raw is None or type(raw) is bool:
        return None
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def _pick_verified_exact_closed_record(
    owned: OwnedShort, records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Only a POSITIVE exact identity match can establish history evidence."""
    seen: set[str] = set()
    matched: dict[str, Any] | None = None
    for item in records:
        identity = str(item.get("positionId") or "")
        if (not identity.isascii() or not identity.isdecimal() or
                int(identity) <= 0 or identity in seen):
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_IDENTITY_DUPLICATE_OR_INVALID"
            )
        if item.get("symbol") != owned.symbol:
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_FILTER_BROKEN"
            )
        seen.add(identity)
        if identity == owned.position_id:
            if matched is not None:
                raise OwnedPositionFinalityHalted(
                    "OWNED_CLOSED_POSITION_AMBIGUOUS"
                )
            matched = item
    if matched is None:
        return None
    p = matched
    if (type(p.get("positionType")) is not int or p["positionType"] != 2 or
            type(p.get("openType")) is not int or p["openType"] != 1 or
            type(p.get("state")) is not int or p["state"] != 3 or
            _decimal0(p.get("holdVol")) != Decimal(0) or
            _decimal0(p.get("closeVol")) != owned.contracts or
            p.get("positionShowStatus") not in (None, "CLOSED")):
        return None
    return p


async def _closed_position_record(
    owned: OwnedShort, probe: ReadOnlyMexcProbe, *,
    page_size: int, max_pages: int,
) -> tuple[dict[str, Any] | None, bool]:
    """(exact closed record, history exhaustive).

    A historical array is useful for a match PRESENT on page 1, but it
    supplies no independent total count. Never paginate it speculatively
    or treat a missing row as evidence that a position did not exist.
    """
    records: list[dict[str, Any]] = []
    expected_count: int | None = None
    expected_pages: int | None = None
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        try:
            response = await probe.raw_history_positions_page(
                owned.symbol, page=page, size=page_size,
            )
        except Exception as exc:
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_GET_OR_SCHEMA_UNVERIFIED"
            ) from exc
        if type(response) is list:
            if page != 1:
                raise OwnedPositionFinalityHalted(
                    "HISTORY_POSITION_RESPONSE_SHAPE_CHANGED"
                )
            return _pick_verified_exact_closed_record(owned, response), False

        count = response["totalCount"]
        declared_pages = response["totalPage"]
        needed = (count + page_size - 1) // page_size
        if (needed != declared_pages or
                (expected_count is not None and count != expected_count) or
                (expected_pages is not None and declared_pages != expected_pages)):
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_PAGINATION_METADATA_DRIFT"
            )
        expected_count, expected_pages = count, declared_pages
        if needed > max_pages:
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_PAGE_LIMIT_UNCERTIFIED"
            )
        if page > max(needed, 1):
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_PAGE_OVERFLOW"
            )
        rows = response["resultList"]
        if len(rows) != min(page_size, max(0, count - (page - 1) * page_size)):
            raise OwnedPositionFinalityHalted(
                "HISTORY_POSITION_PAGE_LENGTH_MISMATCH"
            )
        for item in rows:
            identity = str(item.get("positionId") or "")
            if (not identity.isascii() or not identity.isdecimal() or
                    int(identity) <= 0 or identity in seen):
                raise OwnedPositionFinalityHalted(
                    "HISTORY_POSITION_IDENTITY_DUPLICATE_OR_INVALID"
                )
            if item.get("symbol") != owned.symbol:
                raise OwnedPositionFinalityHalted(
                    "HISTORY_POSITION_FILTER_BROKEN"
                )
            seen.add(identity)
            records.append(item)
        if page >= max(needed, 1):
            break

    if len(records) != expected_count:
        raise OwnedPositionFinalityHalted("HISTORY_POSITION_PAGE_INCOMPLETE")
    return _pick_verified_exact_closed_record(owned, records), True


async def verify_owned_position_finality(
    *, owned: OwnedShort, actual_scope: str, probe: ReadOnlyMexcProbe,
    page_size: int = 100, max_pages: int = 5,
) -> OwnedPositionFinality:
    _validate_owned(owned, actual_scope)
    if not isinstance(probe, ReadOnlyMexcProbe):
        raise OwnedPositionFinalityHalted("STRICT_SIGNED_GET_PROBE_REQUIRED")
    if (type(page_size) is not int or not 1 <= page_size <= 100 or
            type(max_pages) is not int or not 1 <= max_pages <= 5):
        raise OwnedPositionFinalityHalted("INVALID_HISTORY_COLLECTION_BOUNDS")

    try:
        first_hist, first_exhaustive = await _closed_position_record(
            owned, probe, page_size=page_size, max_pages=max_pages)
        open_a = await probe.raw_open_position_by_id(owned.position_id)
        second_hist, second_exhaustive = await _closed_position_record(
            owned, probe, page_size=page_size, max_pages=max_pages)
        open_b = await probe.raw_open_position_by_id(owned.position_id)
    except OwnedPositionFinalityHalted:
        raise
    except Exception as exc:
        raise OwnedPositionFinalityHalted(
            "EXACT_POSITION_OPEN_GET_UNVERIFIED"
        ) from exc

    if open_a or open_b:
        return OwnedPositionFinality("EXACT_OWNED_POSITION_STILL_OPEN")
    if first_exhaustive != second_exhaustive:
        return OwnedPositionFinality("HISTORY_RESPONSE_SHAPE_CHANGED_NOT_CERTIFIED")
    if first_hist is None or second_hist is None:
        return OwnedPositionFinality("EXACT_CLOSED_POSITION_HISTORY_NOT_FOUND")
    critical = (
        "positionId", "symbol", "positionType", "openType",
        "state", "holdVol", "closeVol", "positionShowStatus",
    )
    if tuple(str(first_hist.get(x)) for x in critical) != tuple(
            str(second_hist.get(x)) for x in critical):
        return OwnedPositionFinality("CLOSED_POSITION_HISTORY_DRIFT")

    return OwnedPositionFinality(
        status=("OWNED_POSITION_CLOSED_EXACT_HISTORY_AND_GET_VERIFIED"
                if first_exhaustive else
                "OWNED_POSITION_CLOSED_POSITIVE_ARRAY_MATCH_VERIFIED"),
        closed_position_verified=True,
        historical_pages_exhausted=first_exhaustive,
        open_position_absent_twice=True,
    )
