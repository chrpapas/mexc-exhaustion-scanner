"""Redacted transition diagnostics for finished MEXC TP/SL rows.

Uses authenticated GET data in-process and returns only enum labels, booleans,
counts, and age information. It never returns symbols, IDs, prices, volumes,
or raw timestamps. This is observational evidence only.
"""
from __future__ import annotations

import time
from typing import Any


STATE_LABELS = {
    2: "CANCELED",
    3: "EXECUTED",
    4: "INVALID",
    5: "EXECUTION_FAILED",
}
TRIGGER_LABELS = {
    0: "UNTRIGGERED",
    1: "TAKE_PROFIT",
    2: "STOP_LOSS",
}


def _int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if str(value) == str(raw) else None


def assess_recent_finished_tpsl_transition(
    positions: Any,
    historical_stops: Any,
    *,
    now_ms: int | None = None,
    recent_window_ms: int = 15 * 60 * 1000,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "matching_finished_rows_for_observed_positions": 0,
        "latest_terminal_state": "NONE",
        "latest_trigger_side": "UNKNOWN",
        "latest_is_finished": False,
        "latest_row_recent": False,
        "latest_age_bucket": "UNKNOWN",
        "transition_semantics_observed": False,
        "endpoint_completeness_verified": False,
        "exchange_atomicity_verified": False,
    }
    if (
        not isinstance(positions, list) or not isinstance(historical_stops, list)
        or any(not isinstance(r, dict) for r in positions)
        or any(not isinstance(r, dict) for r in historical_stops)
    ):
        result["status"] = "INVALID_INPUT"
        return result
    if not positions or not historical_stops:
        result["status"] = "INSUFFICIENT_ROWS"
        return result
    if type(recent_window_ms) is not int or not 1000 <= recent_window_ms <= 24 * 60 * 60 * 1000:
        result["status"] = "INVALID_RECENT_WINDOW"
        return result
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if type(now_ms) is not int or isinstance(now_ms, bool):
        result["status"] = "INVALID_NOW"
        return result

    position_ids: set[str] = set()
    for p in positions:
        raw = p.get("positionId") or p.get("id")
        if raw is not None:
            position_ids.add(str(raw))
    if not position_ids:
        result["status"] = "NO_POSITION_IDENTITIES"
        return result

    matching: list[dict[str, Any]] = []
    for row in historical_stops:
        pid = row.get("positionId")
        if pid is not None and str(pid) in position_ids:
            matching.append(row)
    result["matching_finished_rows_for_observed_positions"] = len(matching)
    if not matching:
        result["status"] = "NO_MATCHING_FINISHED_TPSL"
        return result

    def updated(row: dict[str, Any]) -> int:
        return _int(row.get("updateTime")) or _int(row.get("createTime")) or -1

    latest = max(matching, key=updated)
    state = _int(latest.get("state"))
    trigger = _int(latest.get("triggerSide"))
    finished = _int(latest.get("isFinished"))
    ts = updated(latest)

    result["latest_terminal_state"] = STATE_LABELS.get(
        state, "UNKNOWN_TERMINAL_STATE"
    )
    result["latest_trigger_side"] = TRIGGER_LABELS.get(
        trigger, "UNKNOWN_TRIGGER_SIDE"
    )
    result["latest_is_finished"] = finished == 1

    if ts >= 0:
        age = now_ms - ts
        if age < 0:
            result["latest_age_bucket"] = "FUTURE_TIMESTAMP"
        elif age <= 60_000:
            result["latest_age_bucket"] = "LE_1_MIN"
        elif age <= 5 * 60_000:
            result["latest_age_bucket"] = "LE_5_MIN"
        elif age <= recent_window_ms:
            result["latest_age_bucket"] = "RECENT_WINDOW"
        else:
            result["latest_age_bucket"] = "OLDER"
        result["latest_row_recent"] = 0 <= age <= recent_window_ms

    result["transition_semantics_observed"] = (
        result["latest_terminal_state"] in set(STATE_LABELS.values())
        and result["latest_trigger_side"] in set(TRIGGER_LABELS.values())
        and result["latest_is_finished"]
    )
    result["status"] = (
        "RECENT_FINISHED_TPSL_TRANSITION_OBSERVED"
        if result["transition_semantics_observed"] and result["latest_row_recent"]
        else "FINISHED_TPSL_TRANSITION_OBSERVED"
        if result["transition_semantics_observed"]
        else "FINISHED_TPSL_TRANSITION_UNCERTAIN"
    )
    return result
