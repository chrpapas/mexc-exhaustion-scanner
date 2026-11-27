"""Tests historical candidate omissions, time priority and fail-closed behavior."""
import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts import v40_candidate_occupancy_audit as a

S = a.SCENARIO


def csv_text(rows):
    dest = io.StringIO()
    writer = csv.DictWriter(dest, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return dest.getvalue()


def stamp(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def fixture(root, *, unexplained=False, ambiguous=False, corrupt=False):
    # Four first-bar entries. A exits at 01:00, after that bar's
    # admission phase. Missing candidates E/F are initially 4-slot blocked.
    times = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 1800,
             "F": 3600, "G": 9000 if unexplained else 5400}
    if unexplained:
        times["F"] = 5400
    if ambiguous:
        times["F"] = times["G"]
    candidates = [{"symbol": x+"_USDT", "entry_ts":str(t)} for x,t in times.items()]
    accepted = "ABCDG"
    gates = [{"scenario":S,"symbol":x+"_USDT",
              "time":stamp(times[x]),"action":"ADMIT"} for x in accepted]
    entries = [{"scenario":S,"symbol":x+"_USDT",
                "time":stamp(times[x]),"notional":"100"} for x in accepted]
    exits = [{"scenario":S,"symbol":"A_USDT", "entry_time":stamp(0),
              "exit_time":stamp(3600),"kind":"normal_exit","notional":"100"}]
    metrics = [{"scenario":S,"closed":"1","open_positions":"4"}]
    ledger = root/"ledger"
    ledger.mkdir()
    for name,rows in (("entries",entries),("exits",exits),("metrics",metrics)):
        (ledger/(name+".csv")).write_text(csv_text(rows))
    archive = root/"frozen.zip"
    with ZipFile(archive,"w") as z:
        z.writestr("manifest.json", json.dumps(
            {"candidate_count":7,"no_exchange_keys_or_orders":True}))
        z.writestr("candidates.csv",csv_text(candidates))
        z.writestr("capital_2000/gate_decisions.csv",csv_text(gates))
        z.writestr("capital_2000/entries.csv",csv_text(entries))
    if corrupt:
        (ledger/"entries.csv").write_text("invalid")
    return archive,ledger


def run_fixture(archive, ledger):
    with patch.multiple(a,
        ARCHIVE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        EXPECTED_CANDIDATES=7,EXPECTED_GATES=5,EXPECTED_ENTRIES=5,
        EXPECTED_CLOSED=1,EXPECTED_FINAL_SLOTS=4):
        return a.audit_candidate_coverage(archive,ledger)


class CandidateCoverageTests(unittest.TestCase):
    def test_four_slot_missing_gates_are_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_fixture(*fixture(Path(tmp)))
            self.assertTrue(report["omitted_candidate_coverage_verified"])
            self.assertEqual(report["explained_four_slots"],2)
            self.assertEqual(report["exits_applied"],1)
            self.assertEqual(report["remaining_positions"],4)
            self.assertFalse(report["strategy_decisions_independently_replayed"])
            self.assertFalse(report["live_authorization"])

    def test_unexplained_omission_not_certified(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_fixture(*fixture(Path(tmp),unexplained=True))
            self.assertEqual(report["unexplained_omissions"],1)
            self.assertFalse(report["omitted_candidate_coverage_verified"])

    def test_unobserved_same_bar_priority_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_fixture(*fixture(Path(tmp),ambiguous=True))
            self.assertGreater(report["ambiguous_same_bar_order"],0)
            self.assertFalse(report["omitted_candidate_coverage_verified"])

    def test_tampered_entry_list_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(a.CandidateCoverageHalted):
                run_fixture(*fixture(Path(tmp),corrupt=True))

    def test_frozen_archive_hash_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive,ledger = fixture(Path(tmp))
            with self.assertRaisesRegex(a.CandidateCoverageHalted,"FROZEN_ARCHIVE_HASH_MISMATCH"):
                a.audit_candidate_coverage(archive,ledger)
