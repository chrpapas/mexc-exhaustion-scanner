"""Read-only MEXC funding-record discovery with explicit pagination proof.

This module scopes funding reads to an already-observed position ID and maps
MEXC's documented page metadata into the generic strict pagination verifier.
It returns only redacted schema/count evidence to callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
        "semantic_verification_not_part_of_shape_summary": True,
    }


def _decimal(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def assess_funding_economics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Check documented funding direction and fee formula without values.

    MEXC's current funding rules say positive rates: longs pay, shorts receive;
    negative rates: shorts pay, longs receive. This treats the API's signed
    'funding' field as account cashflow and checks whether every non-zero row
    follows that direction, plus whether its magnitude matches
    abs(positionValue * rate) within a conservative rounding tolerance.
    """
    result: dict[str, Any] = {
        "rows_examined": len(rows),
        "nonzero_rows_examined": 0,
        "zero_rate_or_funding_rows": 0,
        "sign_consistent_rows": 0,
        "magnitude_consistent_rows": 0,
        "all_nonzero_rows_sign_consistent": False,
        "all_nonzero_rows_magnitude_consistent": False,
        "funding_cashflow_sign_semantics_observed": False,
        "funding_formula_semantics_observed": False,
    }
    if not rows:
        result["status"] = "NO_FUNDING_ROWS"
        return result

    for row in rows:
        side = row.get("positionType")
        value = _decimal(row.get("positionValue"))
        rate = _decimal(row.get("rate"))
        funding = _decimal(row.get("funding"))
        if (
            isinstance(side, bool) or side not in {1, 2}
            or value is None or value < 0
            or rate is None or funding is None
        ):
            result["status"] = "FUNDING_ROW_SEMANTICS_INVALID"
            return result

        expected = value * rate
        if side == 1:  # long: pays positive funding, receives negative funding
            expected = -expected

        if rate == 0 or funding == 0:
            result["zero_rate_or_funding_rows"] += 1
            if expected == funding:
                result["magnitude_consistent_rows"] += 1
            continue

        result["nonzero_rows_examined"] += 1
        same_sign = (funding > 0) == (expected > 0)
        if same_sign:
            result["sign_consistent_rows"] += 1

        # API decimals can be rounded independently. Require agreement within
        # 1e-8 USDT absolute or 1 ppm relative to the expected fee.
        tolerance = max(Decimal("0.00000001"), abs(expected) * Decimal("0.000001"))
        if abs(funding - expected) <= tolerance:
            result["magnitude_consistent_rows"] += 1

    n = result["nonzero_rows_examined"]
    result["all_nonzero_rows_sign_consistent"] = (
        n > 0 and result["sign_consistent_rows"] == n
    )
    # Magnitude count includes exact zero rows, so compare against all rows.
    result["all_nonzero_rows_magnitude_consistent"] = (
        n > 0 and result["magnitude_consistent_rows"] == len(rows)
    )
    result["funding_cashflow_sign_semantics_observed"] = (
        result["all_nonzero_rows_sign_consistent"]
    )
    result["funding_formula_semantics_observed"] = (
        result["all_nonzero_rows_magnitude_consistent"]
    )
    result["status"] = (
        "FUNDING_SIGN_AND_FORMULA_CONSISTENT"
        if result["funding_cashflow_sign_semantics_observed"]
        and result["funding_formula_semantics_observed"]
        else "FUNDING_SIGN_CONSISTENT_FORMULA_DRIFT"
        if result["funding_cashflow_sign_semantics_observed"]
        else "FUNDING_SIGN_SEMANTIC_MISMATCH"
    )
    return result


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
    rows = list(pages.rows)
    economics = assess_funding_economics(rows)
    return {
        "pagination_verified_for_observed_position": pages.complete,
        "reported_total_rows": pages.total,
        "page_count": pages.page_count,
        "schema": summarize_funding_rows(rows),
        "funding_economics": economics,
        "funding_sign_semantics_verified": bool(
            economics["funding_cashflow_sign_semantics_observed"]
        ),
        "funding_formula_semantics_verified": bool(
            economics["funding_formula_semantics_observed"]
        ),
        "account_wide_funding_completeness_verified": False,
    }
