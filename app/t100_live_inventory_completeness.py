"""Fail-closed current MEXC inventory completeness evidence.

Certifies only CURRENT open-position and CURRENT TP/SL inventory under a
pinned, manually reviewed venue contract. Historical endpoint pagination,
exchange atomicity, fill completeness, and live authorization remain separate.

Reviewed official MEXC Futures documentation (2026-10-26):
- GET /api/v1/private/position/open_positions: optional filters; no pagination
- GET /api/v1/private/stoporder/open_orders: optional symbol; no pagination
- GET /api/v1/private/order/open_order_total_count: stopOrderCount
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CurrentInventoryContractEvidence:
    review_date: str
    open_positions_account_scope_nonpaginated: bool
    current_tpsl_account_scope_nonpaginated: bool
    stop_count_endpoint_documented: bool


MEXC_CURRENT_INVENTORY_CONTRACT_2026_10_26 = CurrentInventoryContractEvidence(
    review_date="2026-10-26",
    open_positions_account_scope_nonpaginated=True,
    current_tpsl_account_scope_nonpaginated=True,
    stop_count_endpoint_documented=True,
)


def _nonnegative_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def assess_current_inventory_completeness(
    *,
    positions: Any,
    stops: Any,
    open_order_counts: Any,
    critical_inventory_double_read_consistent: bool,
    position_margin_witness: Any,
    symbol_scoped_crosscheck: Any,
    contract: CurrentInventoryContractEvidence = MEXC_CURRENT_INVENTORY_CONTRACT_2026_10_26,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "contract_review_date": getattr(contract, "review_date", "UNKNOWN"),
        "documented_open_positions_account_scope_nonpaginated": False,
        "documented_current_tpsl_account_scope_nonpaginated": False,
        "documented_stop_count_endpoint": False,
        "observed_positions": 0,
        "observed_current_tpsl": 0,
        "reported_current_tpsl_count": None,
        "current_tpsl_count_matches": False,
        "critical_inventory_stable": False,
        "position_margin_witness_verified": False,
        "symbol_scoped_redundancy_required": False,
        "symbol_scoped_redundancy_verified": False,
        "current_position_inventory_completeness_verified": False,
        "current_tpsl_inventory_completeness_verified": False,
        "account_wide_completeness_verified": False,
        "historical_stop_pagination_verified": False,
        "exchange_atomicity_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    if (
        not isinstance(contract, CurrentInventoryContractEvidence)
        or not isinstance(positions, list)
        or not isinstance(stops, list)
        or any(not isinstance(row, dict) for row in positions)
        or any(not isinstance(row, dict) for row in stops)
        or not isinstance(open_order_counts, dict)
        or not isinstance(position_margin_witness, dict)
        or not isinstance(symbol_scoped_crosscheck, dict)
        or type(critical_inventory_double_read_consistent) is not bool
    ):
        result["status"] = "CURRENT_INVENTORY_COMPLETENESS_INPUT_AMBIGUOUS"
        return result

    result.update({
        "contract_review_date": contract.review_date,
        "documented_open_positions_account_scope_nonpaginated":
            contract.open_positions_account_scope_nonpaginated,
        "documented_current_tpsl_account_scope_nonpaginated":
            contract.current_tpsl_account_scope_nonpaginated,
        "documented_stop_count_endpoint":
            contract.stop_count_endpoint_documented,
        "observed_positions": len(positions),
        "observed_current_tpsl": len(stops),
        "critical_inventory_stable":
            critical_inventory_double_read_consistent,
    })

    stop_count = _nonnegative_int(open_order_counts.get("stopOrderCount"))
    if stop_count is None:
        result["status"] = "CURRENT_TPSL_COUNT_CONTRACT_INVALID"
        return result
    result["reported_current_tpsl_count"] = stop_count
    result["current_tpsl_count_matches"] = stop_count == len(stops)

    margin_ok = (
        position_margin_witness.get("position_inventory_margin_witness_observed")
        is True
    )
    result["position_margin_witness_verified"] = margin_ok

    symbols_present = bool(positions or stops)
    result["symbol_scoped_redundancy_required"] = symbols_present
    cross_ok = (
        symbol_scoped_crosscheck.get("symbol_scoped_crosscheck_verified") is True
        if symbols_present else True
    )
    result["symbol_scoped_redundancy_verified"] = cross_ok

    positions_complete = all((
        contract.open_positions_account_scope_nonpaginated is True,
        critical_inventory_double_read_consistent,
        margin_ok,
        cross_ok,
    ))
    stops_complete = all((
        contract.current_tpsl_account_scope_nonpaginated is True,
        contract.stop_count_endpoint_documented is True,
        critical_inventory_double_read_consistent,
        result["current_tpsl_count_matches"],
        cross_ok,
    ))

    result["current_position_inventory_completeness_verified"] = positions_complete
    result["current_tpsl_inventory_completeness_verified"] = stops_complete
    result["account_wide_completeness_verified"] = positions_complete and stops_complete

    if result["account_wide_completeness_verified"]:
        result["status"] = "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC"
    elif not critical_inventory_double_read_consistent:
        result["status"] = "CURRENT_INVENTORY_NOT_STABLE"
    elif not margin_ok:
        result["status"] = "POSITION_MARGIN_WITNESS_MISSING"
    elif not result["current_tpsl_count_matches"]:
        result["status"] = "CURRENT_TPSL_COUNT_MISMATCH"
    elif not cross_ok:
        result["status"] = "SYMBOL_SCOPED_REDUNDANCY_MISMATCH"
    else:
        result["status"] = "CURRENT_INVENTORY_CONTRACT_INCOMPLETE"
    return result
