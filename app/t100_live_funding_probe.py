"""Read-only MEXC funding-record discovery with explicit pagination proof.

This module scopes funding reads to an already-observed position ID and maps
MEXC's documented page metadata into the generic strict pagination verifier.
It returns only redacted schema/count evidence to callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_pagination import collect_verified_pages, PaginationHalted
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted
from app.t100_live_contract_evidence import _kind


SAFE_FUNDING_FIELDS = frozenset({
    "id", "symbol", "positionType", "positionValue",
    "funding", "rate", "settleTime",
})


@dataclass
class FundingPageAdapter:
    probe: ReadOnlyMexcProbe
    position_id: str

    async def read_page(self, page: int, size: int) -> dict[str, Any]:
        raw = await self.probe.raw_funding_page(
            self.position_id, page=page, size=size
        )
        page_size = raw.get("pageSize")
        total = raw.get("totalCount")
        total_page = raw.get("totalPage")
        current = raw.get("currentPage")
        rows = raw.get("resultList")
        if any(isinstance(x, bool) or not isinstance(x, int)
               for x in (page_size, total, total_page, current)):
            raise PaginationHalted("FUNDING_PAGE_METADATA_INVALID")
        if page_size < 1 or total < 0 or total_page < 0 or current < 1:
            raise PaginationHalted("FUNDING_PAGE_METADATA_INVALID")
        expected_pages = 0 if total == 0 else (total + page_size - 1) // page_size
        if total == 0:
            if total_page not in {0, 1}:
                raise PaginationHalted("FUNDING_TOTAL_PAGE_MISMATCH")
        elif total_page != expected_pages:
            raise PaginationHalted("FUNDING_TOTAL_PAGE_MISMATCH")
        if current != page:
            raise PaginationHalted("FUNDING_CURRENT_PAGE_MISMATCH")
        return {
            "page_num": current,
            "page_size": page_size,
            "total": total,
            "resultList": rows,
        }


def summarize_funding_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    types: dict[str, set[str]] = {}
    unknown = 0
    for row in rows:
        for key, value in row.items():
            if not isinstance(key, str):
                raise PaginationHalted("FUNDING_NONSTRING_FIELD")
            if key in SAFE_FUNDING_FIELDS:
                types.setdefault(key, set()).add(_kind(value))
            else:
                unknown += 1
    return {
        "family": "funding",
        "shape_only": True,
        "rows_seen": len(rows),
        "allowlisted_field_types": {
            key: sorted(values) for key, values in sorted(types.items())
        },
        "unlisted_field_occurrences": unknown,
        "funding_sign_semantics_verified": False,
    }


async def collect_observed_position_funding(
    probe: ReadOnlyMexcProbe,
    *,
    position_id: str,
    page_size: int = 100,
) -> dict[str, Any]:
    if not isinstance(position_id, str) or not position_id:
        raise VenueReadHalted("INVALID_FUNDING_POSITION_ID")
    pages = await collect_verified_pages(
        FundingPageAdapter(probe, position_id),
        page_size=page_size,
        max_pages=20,
        max_rows=2000,
        identity=lambda row: str(row["id"]) if row.get("id") is not None else "",
    )
    return {
        "pagination_verified_for_observed_position": pages.complete,
        "reported_total_rows": pages.total,
        "page_count": pages.page_count,
        "schema": summarize_funding_rows(list(pages.rows)),
        "funding_sign_semantics_verified": False,
        "account_wide_funding_completeness_verified": False,
    }
