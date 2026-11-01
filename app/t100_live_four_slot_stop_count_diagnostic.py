"""Redacted, offline classification of mismatched current position / TP-SL counts.

Consumes only already-bookended GET rows. No client, SQL, orders, credentials,
symbols, IDs, prices, position sizes, account balances or raw venue records
are emitted. A count mismatch is NEVER proof that a stop is active or absent:
MEXC current TP/SL records can include TP-only rows, multiple rows per
position, or records that do not match the observed position inventory.

This is OBSERVATION ONLY. No stop/exit placement or account authorization.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any


def _identity(raw: Any) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    val = str(raw).strip()
    return val if val and val not in {"0", "None", "null"} else None


def _positive_number(raw: Any) -> bool:
    if raw is None or isinstance(raw, bool):
        return False
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return val.is_finite() and val > 0


def classify_current_position_stop_mismatch(
    positions: Any, current_tpsl: Any,
) -> dict[str, Any]:
    """Summarize raw mismatch by position identity without leaking identity."""
    result: dict[str, Any] = {
        "status": "POSITION_STOP_TOPOLOGY_UNVERIFIED",
        "position_count": None,
        "current_tpsl_row_count": None,
        "positions_without_any_current_tpsl": None,
        "positions_without_positive_stop_loss_row": None,
        "positions_with_multiple_current_tpsl_rows": None,
        "current_tpsl_rows_with_unmatched_position": None,
        "current_tpsl_rows_without_position_id": None,
        "current_tpsl_rows_without_positive_stop_loss": None,
        "current_tpsl_rows_with_positive_stop_loss": None,
        "duplicate_current_tpsl_order_ids": None,
        "position_ids_ambiguous": True,
        "position_stop_count_reconciled": False,
        "native_stop_coverage_certified": False,
        "exchange_atomicity_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }
    if (
        type(positions) is not list
        or type(current_tpsl) is not list
        or any(type(x) is not dict for x in (*positions, *current_tpsl))
        or len(positions) > 1000
        or len(current_tpsl) > 2000
    ):
        return result

    result["position_count"] = len(positions)
    result["current_tpsl_row_count"] = len(current_tpsl)

    pos_ids: list[str] = []
    for row in positions:
        pid = _identity(row.get("positionId") or row.get("id"))
        if pid is None:
            return result
        pos_ids.append(pid)
    if len(pos_ids) != len(set(pos_ids)):
        return result

    pos_set = set(pos_ids)
    result["position_ids_ambiguous"] = False
    by_position: Counter[str] = Counter()
    with_sl: set[str] = set()
    orphan_count = 0
    no_pid_count = 0
    no_sl_count = 0
    yes_sl_count = 0
    stop_ids: list[str] = []

    for row in current_tpsl:
        sid = _identity(row.get("id") or row.get("stopPlanOrderId"))
        if sid is not None:
            stop_ids.append(sid)
        pid = _identity(row.get("positionId"))
        has_stop_loss = _positive_number(row.get("stopLossPrice"))
        if not has_stop_loss:
            no_sl_count += 1
        else:
            yes_sl_count += 1
        if pid is None:
            no_pid_count += 1
        elif pid not in pos_set:
            orphan_count += 1
        else:
            by_position[pid] += 1
            if has_stop_loss:
                with_sl.add(pid)

    result.update({
        "status": "POSITION_STOP_TOPOLOGY_OBSERVED_NONAUTHORIZING",
        "positions_without_any_current_tpsl":
            sum(by_position[x] == 0 for x in pos_ids),
        "positions_without_positive_stop_loss_row":
            sum(x not in with_sl for x in pos_ids),
        "positions_with_multiple_current_tpsl_rows":
            sum(by_position[x] > 1 for x in pos_ids),
        "current_tpsl_rows_with_unmatched_position": orphan_count,
        "current_tpsl_rows_without_position_id": no_pid_count,
        "current_tpsl_rows_without_positive_stop_loss": no_sl_count,
        "current_tpsl_rows_with_positive_stop_loss": yes_sl_count,
        "duplicate_current_tpsl_order_ids":
            len(stop_ids) - len(set(stop_ids)),
    })
    return result
