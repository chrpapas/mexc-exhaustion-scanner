"""Fail-closed strategy-parity certificate for T100/V40 live promotion.

The existing 2026-10-23 V40 export audit proves historical admission/filter
and reference-capacity arithmetic only. It explicitly does NOT prove the full
chronological portfolio, exits, funding, fill path, contract-spec path, or
live execution equivalence.

This module makes that distinction machine-readable. A partial entry-parity
report may pass historical_entry_parity_verified while
strategy_parity_verified remains false unless a stronger full-replay report
contains every required certification claim.
"""
from __future__ import annotations

from typing import Any


FROZEN_V40_ARCHIVE_SHA256 = (
    "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
)
EXPECTED_CANDIDATES = 146
EXPECTED_PRIOR_CANDLES = 7154
EXPECTED_REFERENCE_NOTIONALS = 648

FULL_REPLAY_REQUIRED_TRUE = (
    "chronological_portfolio_replayed",
    "entry_admission_parity_verified",
    "exit_chronology_parity_verified",
    "funding_path_parity_verified",
    "fee_path_parity_verified",
    "contract_spec_point_in_time_parity_verified",
    "lot_rounding_parity_verified",
    "position_occupancy_parity_verified",
    "duplicate_symbol_parity_verified",
    "final_equity_recomputed_independently",
    "drawdown_recomputed_independently",
    "no_forward_fills_or_future_data",
)

FULL_REPLAY_REQUIRED_ZERO = (
    "entry_decision_mismatches",
    "exit_decision_mismatches",
    "funding_mismatches",
    "position_state_mismatches",
    "contract_spec_mismatches",
)


def assess_strategy_parity_report(report: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "historical_entry_parity_verified": False,
        "full_chronological_replay_verified": False,
        "strategy_parity_verified": False,
        "live_execution_equivalence_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    if not isinstance(report, dict):
        result["status"] = "STRATEGY_PARITY_REPORT_INVALID"
        return result

    manifest = report.get("archive_manifest")
    archive_sha = (
        report.get("archive_sha256")
        or (
            manifest.get("archive_sha256")
            if isinstance(manifest, dict)
            else None
        )
    )
    entry_ok = all((
        report.get("status")
        == "V40_ENTRY_FILTER_AND_REFERENCE_CAPACITY_PARITY_PASS",
        archive_sha == FROZEN_V40_ARCHIVE_SHA256,
        report.get("candidates") == EXPECTED_CANDIDATES,
        report.get("prior_candles") == EXPECTED_PRIOR_CANDLES,
        report.get("reference_entry_notionals_exact")
        == EXPECTED_REFERENCE_NOTIONALS,
        report.get("discrepancies") == 0,
    ))
    result["historical_entry_parity_verified"] = entry_ok

    full = report.get("full_replay")
    if not isinstance(full, dict):
        result["status"] = (
            "ENTRY_PARITY_ONLY_FULL_REPLAY_MISSING"
            if entry_ok
            else "STRATEGY_PARITY_EVIDENCE_INCOMPLETE"
        )
        return result

    true_claims = all(full.get(field) is True for field in FULL_REPLAY_REQUIRED_TRUE)
    zero_claims = all(full.get(field) == 0 for field in FULL_REPLAY_REQUIRED_ZERO)

    anchor = full.get("frozen_anchor")
    anchor_ok = (
        isinstance(anchor, dict)
        and anchor.get("stage_rows") == 27776
        and anchor.get("episodes") == 21838
        and anchor.get("multi_stage_ge2") == 4314
        and anchor.get("multi_stage_ge3") == 1172
        and anchor.get("candidate_count") == EXPECTED_CANDIDATES
    )
    result["full_chronological_replay_verified"] = all((
        entry_ok,
        true_claims,
        zero_claims,
        anchor_ok,
        full.get("reviewed_code_revision") == report.get("code_revision"),
        isinstance(report.get("code_revision"), str),
        bool(report.get("code_revision")),
    ))

    # Historical strategy parity is deliberately separate from actual venue
    # execution equivalence. The latter cannot be inferred from a backtest.
    result["live_execution_equivalence_verified"] = (
        report.get("live_execution_equivalence_verified") is True
    )
    result["strategy_parity_verified"] = (
        result["full_chronological_replay_verified"]
    )

    result["status"] = (
        "FULL_STRATEGY_PARITY_VERIFIED_HISTORICAL_ONLY"
        if result["strategy_parity_verified"]
        else "FULL_STRATEGY_PARITY_BLOCKED"
    )
    return result
