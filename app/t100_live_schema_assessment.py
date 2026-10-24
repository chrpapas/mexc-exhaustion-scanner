"""Offline T100 venue schema compatibility diagnostics.

Compares redacted allowlisted field/type evidence against the *current*
shadow snapshot parser expectations. Does not certify real MEXC semantics,
pagination, state enum values, account flatness, or live readiness.
"""
from __future__ import annotations

from typing import Any

REQUIRED = {
    "positions": {
        "symbol": frozenset({"string"}),
        "positionType": frozenset({"integer", "string"}),
        "state": frozenset({"integer", "string"}),
        "holdVol": frozenset({"integer", "number", "string"}),
    },
    "stops": {
        "positionId": frozenset({"string", "integer"}),
        "state": frozenset({"integer", "string"}),
        "stopLossPrice": frozenset({"integer", "number", "string"}),
        "vol": frozenset({"integer", "number", "string"}),
    },
}

IDENTITY_ALTERNATIVES = {
    "positions": (("positionId", "id"),),
    "stops": (("id", "stopPlanOrderId"),),
}


def assess_shadow_schema(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return non-authorizing compatibility flags; no raw venue values."""
    result: dict[str, Any] = {
        "shadow_parser_contract_verified": False,
        "venue_semantics_verified": False,
        "pagination_verified": False,
        "families": {},
        "overall": "UNVERIFIED",
    }
    if not isinstance(evidence, dict):
        result["overall"] = "INVALID_EVIDENCE"
        return result
    statuses: list[str] = []
    for family, required in REQUIRED.items():
        block = evidence.get(family)
        if not isinstance(block, dict) or block.get("family") != family or block.get("shape_only") is not True:
            result["families"][family] = {"status": "INVALID_EVIDENCE"}
            statuses.append("INVALID_EVIDENCE")
            continue
        count = block.get("rows_seen")
        types = block.get("allowlisted_field_types")
        if type(count) is not int or count < 0 or not isinstance(types, dict):
            result["families"][family] = {"status": "INVALID_EVIDENCE"}
            statuses.append("INVALID_EVIDENCE")
            continue
        if count == 0:
            result["families"][family] = {
                "status": "NO_ROWS_SCHEMA_UNKNOWN", "missing_fields": [],
                "incompatible_fields": [],
            }
            statuses.append("NO_ROWS_SCHEMA_UNKNOWN")
            continue
        missing = sorted(set(required) - set(types))
        for alternatives in IDENTITY_ALTERNATIVES[family]:
            if not any(key in types for key in alternatives):
                missing.append("ONE_OF_" + "_OR_".join(alternatives))
        incompatible = sorted(
            key for key, permitted in required.items()
            if key in types and (
                not isinstance(types[key], list) or not types[key]
                or any(t not in permitted for t in types[key])
            )
        )
        for alternatives in IDENTITY_ALTERNATIVES[family]:
            for key in alternatives:
                if key in types and (
                    not isinstance(types[key], list) or not types[key]
                    or any(t not in {"string", "integer"} for t in types[key])
                ):
                    incompatible.append(key)
        incompatible = sorted(set(incompatible))
        status = "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED"
        if missing or incompatible:
            status = "PARSER_SHAPE_MISMATCH"
        result["families"][family] = {
            "status": status,
            "missing_fields": missing,
            "incompatible_fields": incompatible,
        }
        statuses.append(status)
    if "INVALID_EVIDENCE" in statuses:
        result["overall"] = "INVALID_EVIDENCE"
    elif "PARSER_SHAPE_MISMATCH" in statuses:
        result["overall"] = "PARSER_SHAPE_MISMATCH"
    elif "NO_ROWS_SCHEMA_UNKNOWN" in statuses:
        result["overall"] = "INCOMPLETE_SCHEMA_EVIDENCE"
    else:
        result["overall"] = "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED"
    return result
