"""Redacted symbol-scoped redundancy checks for current MEXC inventory.

This does NOT prove account-wide completeness or atomicity. It only checks
that every currently observed aggregate position/TP-SL row is reproducible
through the venue's symbol-filtered GET form with matching safety-critical
fields.
"""
from __future__ import annotations

import json
from typing import Any

from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS,
    CRITICAL_STOP_FIELDS,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


def _canonical_projected(
    rows: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    projected = [
        {key: row[key] for key in fields if key in row}
        for row in rows
    ]
    return tuple(sorted(
        json.dumps(
            row, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        for row in projected
    ))


async def crosscheck_symbol_scoped_inventory(
    probe: ReadOnlyMexcProbe,
    aggregate_positions: Any,
    aggregate_stops: Any,
    *,
    max_symbols: int = 4,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbols_checked": 0,
        "aggregate_positions": 0,
        "aggregate_current_tpsl": 0,
        "position_symbol_scoped_match": False,
        "current_tpsl_symbol_scoped_match": False,
        "symbol_scoped_crosscheck_verified": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
    }

    if (
        not isinstance(aggregate_positions, list)
        or not isinstance(aggregate_stops, list)
        or any(not isinstance(row, dict) for row in aggregate_positions)
        or any(not isinstance(row, dict) for row in aggregate_stops)
    ):
        result["status"] = "INVALID_AGGREGATE_INPUT"
        return result

    result["aggregate_positions"] = len(aggregate_positions)
    result["aggregate_current_tpsl"] = len(aggregate_stops)

    symbols: set[str] = set()
    for row in aggregate_positions:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            result["status"] = "AGGREGATE_POSITION_SYMBOL_MISSING"
            return result
        symbols.add(symbol)

    for row in aggregate_stops:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            result["status"] = "AGGREGATE_STOP_SYMBOL_MISSING"
            return result
        symbols.add(symbol)

    if not symbols:
        result["status"] = "NO_OBSERVED_SYMBOLS"
        return result

    if (
        type(max_symbols) is not int
        or isinstance(max_symbols, bool)
        or max_symbols < 1
        or len(symbols) > max_symbols
    ):
        result["status"] = "BOUNDED_SYMBOL_CROSSCHECK_REFUSED"
        return result

    position_match = True
    stop_match = True

    for symbol in sorted(symbols):
        try:
            filtered_positions = await probe.raw_open_positions(symbol)
            filtered_stops = await probe.raw_open_stops(symbol)
        except Exception:
            result["status"] = "SYMBOL_SCOPED_READ_FAILED"
            return result

        if any(row.get("symbol") != symbol for row in filtered_positions):
            result["status"] = "FILTERED_POSITION_SYMBOL_MISMATCH"
            return result
        if any(
            row.get("symbol") is not None and row.get("symbol") != symbol
            for row in filtered_stops
        ):
            result["status"] = "FILTERED_STOP_SYMBOL_MISMATCH"
            return result

        aggregate_position_rows = [
            row for row in aggregate_positions
            if row.get("symbol") == symbol
        ]
        aggregate_stop_rows = [
            row for row in aggregate_stops
            if row.get("symbol") == symbol
        ]

        try:
            if _canonical_projected(
                filtered_positions, CRITICAL_POSITION_FIELDS
            ) != _canonical_projected(
                aggregate_position_rows, CRITICAL_POSITION_FIELDS
            ):
                position_match = False

            if _canonical_projected(
                filtered_stops, CRITICAL_STOP_FIELDS
            ) != _canonical_projected(
                aggregate_stop_rows, CRITICAL_STOP_FIELDS
            ):
                stop_match = False
        except (TypeError, ValueError, OverflowError):
            result["status"] = "NONCANONICAL_SYMBOL_SCOPED_ROW"
            return result

    result["symbols_checked"] = len(symbols)
    result["position_symbol_scoped_match"] = position_match
    result["current_tpsl_symbol_scoped_match"] = stop_match
    result["symbol_scoped_crosscheck_verified"] = (
        position_match and stop_match
    )
    result["status"] = (
        "SYMBOL_SCOPED_REDUNDANCY_MATCH"
        if result["symbol_scoped_crosscheck_verified"]
        else "SYMBOL_SCOPED_REDUNDANCY_MISMATCH"
    )
    return result
