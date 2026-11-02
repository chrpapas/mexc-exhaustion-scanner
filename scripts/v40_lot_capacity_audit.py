"""Independent exported lot inventory, child-order and prior-bar quota audit.

Recorded fills only: does not prove optimal requested size, absent orders,
current account rights, historical contract rules or executable liquidity.
"""

import csv
import json
import sqlite3
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from scripts.v40_exit_trigger_audit import timestamp


def child_count(quantity, step, minimum, maximum):
    if quantity <= 0 or step <= 0 or quantity % step or minimum <= 0:
        raise ValueError("INVALID_LOT")
    units = quantity / step
    low = (minimum / step).to_integral_value(rounding=ROUND_CEILING)
    if units < low:
        raise ValueError("BELOW_MINIMUM")
    if maximum is None:
        return 1
    high = (maximum / step).to_integral_value(rounding=ROUND_FLOOR)
    if high < low:
        raise ValueError("UNSPLITTABLE_LOTS")
    children = (units / high).to_integral_value(rounding=ROUND_CEILING)
    if units < children * low:
        raise ValueError("UNSPLITTABLE_LOTS")
    return int(children)


def audit(folder, candles_path, snapshot_path):
    payload = json.loads(snapshot_path.read_text())
    if (
        payload.get("NOT_HISTORICAL") is not True
        or payload.get("purpose") != "CURRENT_CONTRACT_SPEC_REFERENCE_ONLY"
    ):
        raise ValueError("SNAPSHOT_PROVENANCE_INVALID")
    specs = payload["specifications"]
    with (folder / "fills.csv").open() as handle:
        fills = list(csv.DictReader(handle))
    db = sqlite3.connect(candles_path.resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    results = {}
    try:
        for cost in (100, 200):
            scenario = f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"
            used = defaultdict(lambda: D(0))
            inventory = defaultdict(lambda: D(0))
            checked = unknown = 0
            for row in (r for r in fills if r["scenario"] == scenario):
                symbol, t = row["symbol"], timestamp(row["time"])
                amount, price = D(row["filled_usdt"]), D(row["price"])
                if not amount.is_finite() or not price.is_finite() or min(amount, price) <= 0:
                    raise ValueError("INVALID_FILL")
                prior = db.execute(
                    "SELECT amount FROM candles WHERE symbol=? AND interval='Min30' AND open_ts=?",
                    (symbol, t - 1800),
                ).fetchall()
                if len(prior) != 1:
                    raise ValueError("PRIOR_BAR_MISSING_OR_DUPLICATE")
                quota = D(str(prior[0][0])) * D(".05")
                used[symbol, t] += amount
                if abs(quota - D(row["quota_total_usdt"])) > D(".001") or used[symbol, t] > quota + D(".001"):
                    raise ValueError("PRIOR_QUOTA_MISMATCH_OR_EXCEEDED")
                if abs(used[symbol, t] - D(row["quota_used_usdt"])) > D(".001"):
                    raise ValueError("CUMULATIVE_QUOTA_MISMATCH")
                spec = specs.get(symbol)
                if spec is None:
                    unknown += 1
                    continue
                quantity = D(row["current_contract_units"])
                size, step, minimum = (D(str(spec[k])) for k in ("contractSize", "volUnit", "minVol"))
                maximum = None if spec.get("maxVol") is None else D(str(spec["maxVol"]))
                children = child_count(quantity, step, minimum, maximum)
                if children != int(row["modeled_child_orders"]) or abs(quantity * size * price - amount) > D(
                    ".001"
                ):
                    raise ValueError("LOT_NOTIONAL_OR_CHILD_COUNT_MISMATCH")
                if row["kind"] == "entry":
                    if inventory[symbol]:
                        raise ValueError("DUPLICATE_LOT_ENTRY")
                    inventory[symbol] = quantity
                elif row["kind"] in ("normal_exit", "delayed_exit", "trim"):
                    inventory[symbol] -= quantity
                    if inventory[symbol] < 0 or (0 < inventory[symbol] < minimum):
                        raise ValueError("OVERCLOSE_OR_UNCLOSABLE_DUST")
                else:
                    raise ValueError("UNKNOWN_FILL_KIND")
                checked += 1
            if not checked:
                raise ValueError("EMPTY_SCENARIO")
            results[str(cost)] = {
                "known_lot_fills": checked,
                "unknown_spec_fills": unknown,
                "quota_groups": len(used),
                "remaining_known_positions": sum(q > 0 for q in inventory.values()),
                "full_sizing_decisions_certified": False,
                "venue_execution_certified": False,
                "live_authorization": False,
            }
    finally:
        db.close()
    return results


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True, type=Path)
    p.add_argument("--candles", required=True, type=Path)
    p.add_argument("--snapshot", required=True, type=Path)
    args = p.parse_args()
    print(json.dumps(audit(args.folder, args.candles, args.snapshot), indent=2))
