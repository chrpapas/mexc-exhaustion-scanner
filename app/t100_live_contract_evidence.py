"""Privacy-preserving shape evidence for authenticated READ-ONLY MEXC probes.

Emits allowlisted schema field names and primitive value types, never
account values, IDs, balances, unknown field names or credential material.
A shape report is discovery evidence, NOT API contract certification.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SAFE_POSITION_FIELDS = frozenset({
    "symbol", "positionId", "id", "positionType", "holdVol",
    "openType", "leverage", "state", "createTime", "updateTime",
    "avgPrice", "holdAvgPrice", "liquidatePrice",
})
SAFE_STOP_FIELDS = frozenset({
    "id", "stopPlanOrderId", "orderId", "positionId", "symbol", "state",
    "positionType", "lossTrend", "profitTrend", "triggerSide",
    "stopLossPrice", "takeProfitPrice", "vol", "realityVol",
    "placeOrderId", "errorCode", "isFinished", "version", "priceProtect",
    "profitLossVolType", "takeProfitVol", "stopLossVol",
    "createTime", "updateTime", "volType", "takeProfitReverse",
    "stopLossReverse", "takeProfitType", "takeProfitOrderPrice",
    "stopLossType", "stopLossOrderPrice",
})


class ContractEvidenceHalted(RuntimeError):
    pass


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unsupported"


def summarize_rows(rows: Any, *, family: str) -> dict[str, Any]:
    if family not in {"positions", "stops"}:
        raise ContractEvidenceHalted("UNKNOWN_SCHEMA_FAMILY")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ContractEvidenceHalted("MALFORMED_SCHEMA_ROWS")
    allow = SAFE_POSITION_FIELDS if family == "positions" else SAFE_STOP_FIELDS
    types: dict[str, set[str]] = {}
    unknown_fields = 0
    for row in rows:
        for key, value in row.items():
            if not isinstance(key, str):
                raise ContractEvidenceHalted("NONSTRING_FIELD_NAME")
            if key in allow:
                types.setdefault(key, set()).add(_kind(value))
            else:
                unknown_fields += 1
    return {
        "family": family,
        "shape_only": True,
        "contract_verified": False,
        "rows_seen": len(rows),
        "allowlisted_field_types": {
            key: sorted(values) for key, values in sorted(types.items())
        },
        "unlisted_field_occurrences": unknown_fields,
        "empty_rows_cannot_establish_schema": len(rows) == 0,
    }


def summarize_inventory_schema(positions: Any, stops: Any) -> dict[str, Any]:
    return {
        "positions": summarize_rows(positions, family="positions"),
        "stops": summarize_rows(stops, family="stops"),
        "note": "REDACTED_SHAPE_ONLY_NOT_ATOMICITY_OR_PAGINATION_EVIDENCE",
    }
