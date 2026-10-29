"""Coherent GET-only current-account inventory completeness probe.

Sequence:
1. account-wide current positions + current TP/SL
2. symbol-scoped redundancy against that inventory
3. account assets / position-margin witness
4. account in-flight order counts / stopOrderCount witness
5. account-wide current positions + current TP/SL again

The first and final safety-critical projections must match. This gives a
bookended evidence window, but it is still not exchange atomicity.
No mutations or DB writes occur.
"""
from __future__ import annotations

import json
from typing import Any

from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS,
    CRITICAL_STOP_FIELDS,
)
from app.t100_live_inventory_completeness import (
    assess_current_inventory_completeness,
)
from app.t100_live_position_margin_witness import (
    assess_position_margin_witness,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_symbol_crosscheck import (
    crosscheck_symbol_scoped_inventory,
)


def _canonical(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(
        json.dumps(
            {key: row[key] for key in fields if key in row},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in rows
    ))


async def run_current_inventory_completeness_probe(
    probe: ReadOnlyMexcProbe,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "read_only": True,
        "live_order_submission_possible": False,
        "exchange_atomicity_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    try:
        before_positions = await probe.raw_open_positions()
        before_stops = await probe.raw_open_stops()

        crosscheck = await crosscheck_symbol_scoped_inventory(
            probe,
            before_positions,
            before_stops,
        )
        assets = await probe.raw_account_assets()
        margin = assess_position_margin_witness(
            before_positions,
            assets,
        )
        counts = await probe.raw_open_order_counts()

        after_positions = await probe.raw_open_positions()
        after_stops = await probe.raw_open_stops()

        stable_positions = (
            _canonical(before_positions, CRITICAL_POSITION_FIELDS)
            == _canonical(after_positions, CRITICAL_POSITION_FIELDS)
        )
        stable_stops = (
            _canonical(before_stops, CRITICAL_STOP_FIELDS)
            == _canonical(after_stops, CRITICAL_STOP_FIELDS)
        )
        critical_stable = stable_positions and stable_stops

        certificate = assess_current_inventory_completeness(
            positions=before_positions,
            stops=before_stops,
            open_order_counts=counts,
            critical_inventory_double_read_consistent=critical_stable,
            position_margin_witness=margin,
            symbol_scoped_crosscheck=crosscheck,
        )
        result.update({
            "before_positions": len(before_positions),
            "after_positions": len(after_positions),
            "before_current_tpsl": len(before_stops),
            "after_current_tpsl": len(after_stops),
            "critical_positions_equal_across_witness_window":
                stable_positions,
            "critical_current_tpsl_equal_across_witness_window":
                stable_stops,
            "critical_inventory_equal_across_witness_window":
                critical_stable,
            "symbol_scoped_inventory_crosscheck": crosscheck,
            "position_inventory_economic_witness": margin,
            "open_order_counts": {
                "sumCount": counts["sumCount"],
                "limitOrderCount": counts["limitOrderCount"],
                "stopOrderCount": counts["stopOrderCount"],
                "planOrderCount": counts["planOrderCount"],
                "trackOrderCount": counts["trackOrderCount"],
            },
            "current_inventory_completeness": certificate,
            "account_wide_completeness_verified": certificate[
                "account_wide_completeness_verified"
            ],
            "status": certificate["status"],
        })
        return result
    except Exception as exc:
        result.update({
            "status": "CURRENT_INVENTORY_COMPLETENESS_PROBE_HALTED",
            "failure_type": type(exc).__name__,
            "account_wide_completeness_verified": False,
        })
        return result
