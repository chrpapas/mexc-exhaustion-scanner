"""Read-only, fail-closed venue pagination verifier for shadow inventory.

This module validates an explicit metadata-bearing pagination contract from
a *read-only* transport; it never invents completeness from a short or empty
page, or sends POST requests.

Important: current MEXC documentation for
/api/v1/private/stoporder/list/orders describes data as a bare list and does
not report total/page metadata. Therefore this verifier MUST NOT be applied
directly to that endpoint unless MEXC later provides independently observed
metadata that satisfies this contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Callable


class PaginationHalted(RuntimeError):
    pass


class PageReader(Protocol):
    async def read_page(self, page: int, size: int) -> Any: ...


@dataclass(frozen=True)
class CompletePages:
    rows: tuple[dict[str, Any], ...]
    total: int
    page_count: int
    complete: bool = True


def _int(raw: Any, field: str, *, min_value: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < min_value:
        raise PaginationHalted(f"INVALID_{field}")
    return raw


async def collect_verified_pages(
    reader: PageReader, *, page_size: int = 100, max_pages: int = 20,
    max_rows: int = 2000, identity: Callable[[dict[str, Any]], str] | None = None,
) -> CompletePages:
    """Require {page_num,page_size,total,resultList} on every response.

    This is an internal strict contract, not a claim that current MEXC
    endpoints expose these exact names. An adapter must be independently
    verified against real read-only responses before production use.
    """
    _int(page_size, "PAGE_SIZE", min_value=1)
    _int(max_pages, "MAX_PAGES", min_value=1)
    _int(max_rows, "MAX_ROWS", min_value=1)
    if page_size > 100 or max_pages > 100 or max_rows > 10000:
        raise PaginationHalted("UNSAFE_PAGINATION_BOUNDS")
    if identity is None:
        raise PaginationHalted("ROW_IDENTITY_REQUIRED")
    seen: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    pages = 0
    for page in range(1, max_pages + 1):
        try:
            raw = await reader.read_page(page, page_size)
        except PaginationHalted:
            raise
        except Exception as exc:
            raise PaginationHalted("PAGE_READ_FAILED") from exc
        if not isinstance(raw, dict):
            raise PaginationHalted("PAGE_RESPONSE_MALFORMED")
        reported_page = _int(raw.get("page_num"), "REPORTED_PAGE", min_value=1)
        reported_size = _int(raw.get("page_size"), "REPORTED_SIZE", min_value=1)
        total = _int(raw.get("total"), "REPORTED_TOTAL")
        rows = raw.get("resultList")
        if reported_page != page or reported_size != page_size:
            raise PaginationHalted("PAGE_METADATA_MISMATCH")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PaginationHalted("PAGE_ROWS_MALFORMED")
        if len(rows) > page_size:
            raise PaginationHalted("PAGE_OVERFLOW")
        if expected_total is None:
            expected_total = total
            if total > max_rows or total > page_size * max_pages:
                raise PaginationHalted("PAGINATION_LIMIT_EXCEEDED")
        elif total != expected_total:
            raise PaginationHalted("TOTAL_CHANGED_DURING_PAGINATION")
        for row in rows:
            try:
                key = identity(row)
            except Exception as exc:
                raise PaginationHalted("ROW_IDENTITY_INVALID") from exc
            if not isinstance(key, str) or not key or key in seen:
                raise PaginationHalted("MISSING_OR_DUPLICATE_ROW_IDENTITY")
            seen.add(key)
        all_rows.extend(rows)
        pages += 1
        if len(all_rows) > total:
            raise PaginationHalted("PAGE_COUNT_EXCEEDS_TOTAL")
        if len(all_rows) == total:
            # Stable count and distinct IDs still do NOT prove atomicity.
            # Caller must independently establish a consistent venue snapshot.
            return CompletePages(tuple(all_rows), total, pages)
        if not rows or len(rows) != page_size:
            raise PaginationHalted("INCOMPLETE_PAGE_BEFORE_TOTAL")
    raise PaginationHalted("PAGE_LIMIT_EXHAUSTED")
