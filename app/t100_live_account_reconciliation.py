"""Fail-closed T100 account reconciliation policy.

Exchange atomicity is not assumed. Reconciliation is permitted only from a
coherent current-inventory completeness certificate and one of two explicit
states:

1. FLAT: no positions, no TP/SL and no in-flight orders.
2. MANAGED NON-FLAT: durable non-flat recovery has independently reconstructed
   every managed position, and there are no unrelated limit/plan/trailing
   orders.

This policy never authorizes live trading by itself.
"""
from __future__ import annotations
from typing import Any


def assess_account_reconciliation(
    completeness_probe: Any,
    *,
    durable_nonflat_recovery_verified: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "account_reconciled": False,
        "flat_account_reconciled": False,
        "managed_nonflat_account_reconciled": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
        # Independent nonauthorizing risk flags are collected only after
        # account-wide current-inventory evidence is coherent. They do not
        # relax the original short-circuit reconciliation gates.
        "observed_nonflat_without_current_tpsl": False,
        "observed_unrelated_limit_orders": None,
        "reconciliation_blockers": [],
        "entry_allowed": False,
        "live_authorization": False,
    }
    if (
        not isinstance(completeness_probe, dict)
        or type(durable_nonflat_recovery_verified) is not bool
    ):
        result["status"] = "ACCOUNT_RECONCILIATION_INPUT_AMBIGUOUS"
        return result

    completeness = (
        completeness_probe.get("account_wide_completeness_verified") is True
    )
    stable = (
        completeness_probe.get(
            "critical_inventory_equal_across_witness_window"
        )
        is True
    )
    result["account_wide_completeness_verified"] = completeness
    if not completeness or not stable:
        result["status"] = "ACCOUNT_INVENTORY_NOT_CERTIFIED_STABLE"
        return result

    positions = completeness_probe.get("after_positions")
    stops = completeness_probe.get("after_current_tpsl")
    counts = completeness_probe.get("open_order_counts")
    if (
        isinstance(positions, bool)
        or not isinstance(positions, int)
        or positions < 0
        or isinstance(stops, bool)
        or not isinstance(stops, int)
        or stops < 0
        or not isinstance(counts, dict)
    ):
        result["status"] = "ACCOUNT_RECONCILIATION_COUNTS_INVALID"
        return result

    required = (
        "sumCount", "limitOrderCount", "stopOrderCount",
        "planOrderCount", "trackOrderCount",
    )
    if any(
        isinstance(counts.get(k), bool)
        or not isinstance(counts.get(k), int)
        or counts[k] < 0
        for k in required
    ):
        result["status"] = "ACCOUNT_RECONCILIATION_COUNTS_INVALID"
        return result

    if counts["stopOrderCount"] != stops:
        result["status"] = "ACCOUNT_TPSL_COUNT_DRIFT"
        return result

    unrelated = (
        counts["limitOrderCount"]
        + counts["planOrderCount"]
        + counts["trackOrderCount"]
    )
    # Do not allow a benign-looking unrelated limit order to obscure an
    # observed protection gap in the same coherent account snapshot.
    # Conversely, no single zero-stop count proves an alternative external
    # protection mechanism is absent. This is CURRENT native-TP/SL evidence.
    missing_native = positions > 0 and stops == 0
    result["observed_nonflat_without_current_tpsl"] = missing_native
    result["observed_unrelated_limit_orders"] = counts["limitOrderCount"]
    result["unrelated_inflight_orders"] = unrelated
    blockers = []
    if missing_native:
        blockers.append("NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL")
    if unrelated:
        blockers.append("UNRELATED_INFLIGHT_ORDERS_PRESENT")
    if positions > 0 and durable_nonflat_recovery_verified is not True:
        blockers.append("DURABLE_NONFLAT_RECOVERY_UNVERIFIED")
    if positions == 0 and stops > 0:
        blockers.append("TPSL_WITHOUT_OBSERVED_OPEN_POSITION")
    result["reconciliation_blockers"] = blockers
    if unrelated:
        result["status"] = "UNRELATED_INFLIGHT_ORDERS_PRESENT"
        return result

    if positions == 0 and stops == 0:
        if counts["sumCount"] != 0:
            result["status"] = "FLAT_ACCOUNT_ORDER_COUNT_NONZERO"
            return result
        result["flat_account_reconciled"] = True
        result["account_reconciled"] = True
        result["status"] = "FLAT_ACCOUNT_RECONCILED_NONATOMIC"
        return result

    if positions <= 0 or stops <= 0:
        result["status"] = "NONFLAT_PROTECTION_INVENTORY_INCOHERENT"
        return result
    if durable_nonflat_recovery_verified is not True:
        result["status"] = "NONFLAT_DURABLE_RECOVERY_REQUIRED"
        return result
    if counts["sumCount"] != counts["stopOrderCount"]:
        result["status"] = "NONFLAT_UNRELATED_ORDER_COUNT_PRESENT"
        return result

    result["managed_nonflat_account_reconciled"] = True
    result["account_reconciled"] = True
    result["status"] = "MANAGED_NONFLAT_ACCOUNT_RECONCILED_NONATOMIC"
    return result
