import csv
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from scripts.v40_lot_capacity_audit import audit, child_count


class LotCapacityAuditTests(unittest.TestCase):
    def test_child_partition_requires_each_child_to_meet_minimum(self):
        self.assertEqual(child_count(D(12), D(1), D(4), D(5)), 3)
        with self.assertRaisesRegex(ValueError, "UNSPLITTABLE"):
            child_count(D(11), D(1), D(4), D(5))
        with self.assertRaisesRegex(ValueError, "INVALID_LOT"):
            child_count(D("4.5"), D(1), D(4), D(5))

    def test_prior_bar_quota_and_exact_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbpath = root / "candles.sqlite"
            with sqlite3.connect(dbpath) as db:
                db.execute("CREATE TABLE candles(symbol TEXT, interval TEXT, open_ts INTEGER, amount REAL)")
                db.executemany(
                    "INSERT INTO candles VALUES(?,?,?,?)",
                    [("A_USDT", "Min30", -1800, 200), ("A_USDT", "Min30", 0, 200)],
                )
            spec = root / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "NOT_HISTORICAL": True,
                        "purpose": "CURRENT_CONTRACT_SPEC_REFERENCE_ONLY",
                        "specifications": {
                            "A_USDT": {"contractSize": 1, "volUnit": 1, "minVol": 2, "maxVol": 3}
                        },
                    }
                )
            )
            rows = []
            for cost in (100, 200):
                for kind, time in [
                    ("entry", "1970-01-01T00:00:00+00:00"),
                    ("normal_exit", "1970-01-01T00:30:00+00:00"),
                ]:
                    rows.append(
                        {
                            "scenario": f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID",
                            "symbol": "A_USDT",
                            "time": time,
                            "kind": kind,
                            "filled_usdt": "8",
                            "price": "2",
                            "quota_total_usdt": "10",
                            "quota_used_usdt": "8",
                            "current_contract_units": "4",
                            "modeled_child_orders": "2",
                        }
                    )

            def write():
                with (root / "fills.csv").open("w") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)

            write()
            self.assertEqual(audit(root, dbpath, spec)["100"]["remaining_known_positions"], 0)
            # A future candle's larger turnover cannot justify the preceding fill.
            rows[0]["quota_total_usdt"] = "1000"
            write()
            with self.assertRaisesRegex(ValueError, "QUOTA"):
                audit(root, dbpath, spec)
            rows[0]["quota_total_usdt"] = "10"
            rows[1].update(
                filled_usdt="6", quota_used_usdt="6", current_contract_units="3", modeled_child_orders="1"
            )
            write()
            with self.assertRaisesRegex(ValueError, "DUST"):
                audit(root, dbpath, spec)
