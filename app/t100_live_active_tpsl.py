"""Privacy-preserving semantic checks for current MEXC TP/SL rows.

Consumes raw authenticated GET results in-process but returns only booleans
and counts. It never returns symbols, IDs, prices, quantities, or row data.
Passing these checks is evidence about the observed rows only; it does not
prove endpoint completeness, atomicity, or live-trading readiness.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _integer(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if str(value) == str(raw) else None


def _positive(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def assess_active_tpsl_semantics(
    positions: Any, stops: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "observed_positions": 0,
        "observed_current_tpsl_rows": 0,
        "partial_position_tpsl_rows": 0,
        "entire_position_tpsl_rows": 0,
        "same_volume_tpsl_rows": 0,
        "separate_volume_tpsl_rows": 0,
        "unknown_vol_type_rows": 0,
        "unknown_profit_loss_vol_type_rows": 0,
        "all_rows_state_untriggered": False,
        "all_rows_nonfinal_if_field_present": False,
        "all_rows_link_to_observed_position": False,
        "all_rows_symbol_link_consistent": False,
        "all_rows_position_side_consistent_if_field_present": False,
        "all_rows_positive_stop_loss_price": False,
        "all_rows_positive_protective_volume": False,
        "all_rows_effective_protective_quantity_verified": False,
        "every_observed_position_has_current_tpsl": False,
        "observed_stop_loss_volume_covers_positions": False,
        "active_tpsl_row_semantics_verified": False,
        "endpoint_completeness_verified": False,
        "exchange_atomicity_verified": False,
    }
    if (
        not isinstance(positions, list) or not isinstance(stops, list)
        or any(not isinstance(r, dict) for r in positions)
        or any(not isinstance(r, dict) for r in stops)
    ):
        result["status"] = "INVALID_INPUT"
        return result

    result["observed_positions"] = len(positions)
    result["observed_current_tpsl_rows"] = len(stops)
    if not positions or not stops:
        result["status"] = "INSUFFICIENT_OBSERVED_ROWS"
        return result

    pos_by_id: dict[str, dict[str, Any]] = {}
    position_qty: dict[str, Decimal] = {}
    for p in positions:
        pid_raw = p.get("positionId") or p.get("id")
        pid = str(pid_raw) if pid_raw is not None else ""
        qty = _positive(p.get("holdVol"))
        if not pid or qty is None or pid in pos_by_id:
            result["status"] = "INVALID_POSITION_ROWS"
            return result
        pos_by_id[pid] = p
        position_qty[pid] = qty

    linked: set[str] = set()
    protective_by_position: dict[str, Decimal] = {}
    state_ok = True
    nonfinal_ok = True
    link_ok = True
    symbol_ok = True
    side_ok = True
    price_ok = True
    volume_ok = True
    effective_quantity_ok = True

    for s in stops:
        vol_type = _integer(s.get("volType"))
        if vol_type == 1:
            result["partial_position_tpsl_rows"] += 1
        elif vol_type == 2:
            result["entire_position_tpsl_rows"] += 1
        else:
            result["unknown_vol_type_rows"] += 1

        profit_loss_vol_type = s.get("profitLossVolType")
        if profit_loss_vol_type == "SAME":
            result["same_volume_tpsl_rows"] += 1
        elif profit_loss_vol_type == "SEPARATE":
            result["separate_volume_tpsl_rows"] += 1
        else:
            result["unknown_profit_loss_vol_type_rows"] += 1

        pid_raw = s.get("positionId")
        pid = str(pid_raw) if pid_raw is not None else ""
        p = pos_by_id.get(pid)
        if not pid or p is None:
            link_ok = False
        else:
            linked.add(pid)

        if _integer(s.get("state")) != 1:
            state_ok = False

        # MEXC documents isFinished=0 for non-final rows. Require the field
        # for this stronger semantic signal rather than inferring it merely
        # because the row came from /open_orders.
        if "isFinished" not in s or _integer(s.get("isFinished")) != 0:
            nonfinal_ok = False

        if p is None:
            symbol_ok = False
            side_ok = False
        else:
            if s.get("symbol") != p.get("symbol"):
                symbol_ok = False
            if "positionType" not in s:
                side_ok = False
            elif _integer(s.get("positionType")) != _integer(p.get("positionType")):
                side_ok = False

        if _positive(s.get("stopLossPrice")) is None:
            price_ok = False

        protective_raw = s.get("vol")
        if profit_loss_vol_type == "SEPARATE":
            protective_raw = s.get("stopLossVol")
        protective = _positive(protective_raw)
        if protective is None:
            volume_ok = False

        # MEXC documents volType=2 as position TP/SL, and its current
        # product guidance says Entire Position TP/SL tracks the whole
        # position quantity automatically. Real read-only evidence also
        # shows a volType=2 + SAME row without a positive explicit volume.
        # Infer effective stop coverage only for that exact whole-position
        # mode. Partial TP/SL and SEPARATE quantity modes still require an
        # explicit positive stop-loss quantity.
        whole_position_dynamic = (
            vol_type == 2 and profit_loss_vol_type == "SAME"
        )
        effective_protective = protective
        if whole_position_dynamic and p is not None:
            effective_protective = position_qty.get(pid)

        if effective_protective is None:
            effective_quantity_ok = False
        elif p is not None:
            protective_by_position[pid] = (
                protective_by_position.get(pid, Decimal(0))
                + effective_protective
            )

    result["all_rows_state_untriggered"] = state_ok
    result["all_rows_nonfinal_if_field_present"] = nonfinal_ok
    result["all_rows_link_to_observed_position"] = link_ok
    result["all_rows_symbol_link_consistent"] = symbol_ok
    result["all_rows_position_side_consistent_if_field_present"] = side_ok
    result["all_rows_positive_stop_loss_price"] = price_ok
    result["all_rows_positive_protective_volume"] = volume_ok
    result["all_rows_effective_protective_quantity_verified"] = (
        effective_quantity_ok
    )
    result["every_observed_position_has_current_tpsl"] = (
        set(pos_by_id) == linked
    )
    result["observed_stop_loss_volume_covers_positions"] = all(
        protective_by_position.get(pid, Decimal(0)) >= qty
        for pid, qty in position_qty.items()
    )

    result["active_tpsl_row_semantics_verified"] = all((
        state_ok,
        nonfinal_ok,
        link_ok,
        symbol_ok,
        side_ok,
        price_ok,
        effective_quantity_ok,
        result["every_observed_position_has_current_tpsl"],
        result["observed_stop_loss_volume_covers_positions"],
    ))
    result["status"] = (
        "OBSERVED_CURRENT_TPSL_SEMANTICS_COMPATIBLE_UNCERTIFIED"
        if result["active_tpsl_row_semantics_verified"]
        else "OBSERVED_CURRENT_TPSL_SEMANTIC_MISMATCH"
    )
    return result
