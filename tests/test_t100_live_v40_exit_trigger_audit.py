import unittest
import csv
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.v40_exit_trigger_audit import audit_position, audit, open_candle_database


def exit_row(time, price, reason, kind='normal_exit', amount='100'):
    return {'exit_time': time, 'exit': str(price), 'reason': reason, 'kind': kind, 'notional': amount}


class ExitTriggerAuditTests(unittest.TestCase):
    def test_adverse_stop_precedes_same_bar_trail_activation(self):
        result = audit_position(100, 100, [(0, 180, 190, 80, 90)], [
            exit_row('1970-01-01T00:00:00+00:00', 180, 'pretrail_stop')])
        self.assertEqual(result['triggers'], 1)

    def test_new_low_only_changes_next_bar_stop_and_partial_exit_stays_pending(self):
        result = audit_position(100, 100, [
            (0, 100, 110, 80, 90),
            (1800, 80, 81, 70, 75),
            (3600, 95, 100, 90, 96),
        ], [
            exit_row('1970-01-01T00:30:00+00:00', '80.5', 'trail_partial', amount='40'),
            exit_row('1970-01-01T01:00:00+00:00', 95, 'delayed_forced_exit', 'delayed_exit', '60'),
        ])
        self.assertEqual(result['normal_fills'], 1)
        self.assertEqual(result['delayed_fills'], 1)
        self.assertEqual(result['remaining_notional'], '0')
        self.assertFalse(result['pending'])

    def test_rejects_lookahead_and_invented_delayed_exit(self):
        for row in (
            exit_row('1970-01-01T00:00:00+00:00', '80.5', 'trail'),
            exit_row('1970-01-01T00:00:00+00:00', 100, 'delayed_forced_exit', 'delayed_exit'),
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                audit_position(100, 100, [(0, 100, 110, 80, 90)], [row])

    def test_rejects_optimistic_gap_fill_and_candle_gap(self):
        with self.assertRaisesRegex(ValueError, 'EXIT_PRICE_MISMATCH'):
            audit_position(100, 100, [(0, 180, 190, 90, 185)], [
                exit_row('1970-01-01T00:00:00+00:00', 175, 'pretrail_stop')])
        with self.assertRaisesRegex(ValueError, 'CANDLE_GAP'):
            audit_position(100, 100, [(0, 100, 110, 90, 100), (3600, 100, 110, 90, 100)], [])

class ImmutableSqliteExitAuditTests(unittest.TestCase):
    def _create_sqlite(self, root):
        path = root / "historical candles.sqlite"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE candles (symbol TEXT, interval TEXT, open_ts INTEGER,"
                "open REAL, high REAL, low REAL, close REAL)"
            )
            db.execute(
                "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                ("TEST_USDT", "Min30", 1790323200, 100, 180, 90, 170),
            )
        return path

    def test_immutable_copy_can_be_read_without_sql_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._create_sqlite(Path(tmp))
            with open_candle_database(db_path, immutable=True) as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM candles"
                ).fetchone()[0], 1)
                self.assertEqual(db.execute(
                    "PRAGMA query_only"
                ).fetchone()[0], 1)
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute("DELETE FROM candles")

    def test_immutable_mode_does_not_ignore_side_file_content(self):
        for suffix in ("-wal", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                path = self._create_sqlite(Path(tmp))
                Path(str(path) + suffix).write_bytes(b"pending")
                with self.assertRaisesRegex(
                    ValueError, "IMMUTABLE_CANDLE_DB_HAS_PENDING_JOURNAL"
                ):
                    open_candle_database(path, immutable=True)

    def test_missing_file_cannot_create_blank_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-here.sqlite"
            with self.assertRaisesRegex(ValueError, "HISTORICAL_CANDLE_DATABASE_MISSING"):
                open_candle_database(missing, immutable=True)
            self.assertFalse(missing.exists())

    def test_full_exit_audit_on_immutable_candle_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = self._create_sqlite(root)
            out = root / "reference"
            out.mkdir()
            entry_time = "2026-09-25T08:00:00+00:00"
            # First candle timestamp is 2026-09-25 08:00 UTC.
            instant = int(datetime.fromisoformat(entry_time).timestamp())
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE candles SET open_ts=?", (instant,))
            def write(name, fields, rows):
                with (out / (name + ".csv")).open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            scenarios = [
                f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"
                for cost in (100, 200)
            ]
            write("entries", ("scenario","symbol","time","notional"),
                  [dict(scenario=s,symbol="TEST_USDT",
                        time=entry_time,notional="100") for s in scenarios])
            write("exits", ("scenario","symbol","entry_time","exit_time",
                            "kind","notional","reason","exit"),
                  [dict(scenario=s,symbol="TEST_USDT",
                        entry_time=entry_time,exit_time=entry_time,
                        kind="normal_exit",notional="100",
                        reason="pretrail_stop",exit="175") for s in scenarios])
            write("fills", ("scenario","symbol","time","kind","price"),
                  [dict(scenario=s,symbol="TEST_USDT",
                        time=entry_time,kind="entry",price="100")
                   for s in scenarios])
            result = audit(out,db_path,instant,immutable=True)
            self.assertEqual(set(result), {"100", "200"})
            for row in result.values():
                self.assertEqual(row["positions"], 1)
                self.assertEqual(row["normal_fills"], 1)
                self.assertEqual(row["triggers"], 1)
                self.assertFalse(row["full_strategy_parity_certified"])
                self.assertFalse(row["live_authorization"])
