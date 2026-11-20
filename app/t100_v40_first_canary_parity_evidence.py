"""Read the archived October 2026 entry audit shipped in the deployed wheel.

Original V40 entry parity is a fixed historical artifact, not a future
valuation or full exit/funding replay. Never invent an audit report from the
expected numbers when the original artifact is missing or changed.
"""
from __future__ import annotations
from importlib.resources import files
import json
from typing import Any

from app.t100_live_strategy_parity_gate import (
    assess_v40_first_canary_entry_parity_report,
    FROZEN_V40_ARCHIVE_SHA256,
)

_DOCUMENT="v40_frozen_entry_parity_evidence.json"


class V40EntryParityEvidenceHalted(RuntimeError):
    pass


def load_verified_frozen_v40_entry_parity()->dict[str,Any]:
    try:
        raw=files("app").joinpath(_DOCUMENT).read_text(encoding="utf-8")
        report=json.loads(raw)
        if type(report) is not dict:
            raise ValueError
        if report.get("archive_sha256")!=FROZEN_V40_ARCHIVE_SHA256:
            raise ValueError
        # Explicit rejection of unreviewed live execution claims. Historical
        # entry admission and reference capacity are the ONLY certified scope.
        limitations=report.get("limitations")
        if (
            type(limitations) is not list
            or not any("No live order" in str(x) for x in limitations)
            or report.get("archive_manifest",{}).get("type")
                !="READ_ONLY_SOURCE_PARITY_SAMPLE"
            or report.get("direct_policy_reference_notionals_exact")!=648
        ):
            raise ValueError
        assessed=assess_v40_first_canary_entry_parity_report(report)
        if (
            assessed["first_canary_entry_parity_verified"] is not True
            or assessed["live_authorization"] is not False
            or assessed["entry_allowed"] is not False
        ):
            raise ValueError
        return report
    except Exception:
        raise V40EntryParityEvidenceHalted(
            "V40_ORIGINAL_FROZEN_ENTRY_PARITY_REPORT_MISSING_OR_INVALID"
        ) from None
