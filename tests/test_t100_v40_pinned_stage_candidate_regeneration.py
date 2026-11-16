"""Synthetic offline tests for SHA-pinned V40 candidate regeneration."""
import csv
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from scripts import v40_pinned_stage_candidate_regeneration as audit


def rows_to_csv(rows):
    stream = io.StringIO()
    w = csv.DictWriter(stream, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return stream.getvalue()


def original_research_fixture(base):
    src = base / "source"
    src.mkdir()
    stage = base / "episode_stage_dataset.csv"
    stage.write_text("v2 synthetic stage fixture only\n")
    stage_module = src / "stage_reference.py"
    stage_module.write_text(
        "from types import SimpleNamespace\n"
        "def load_exact_v2_stage_dataset(path):\n"
        " return [SimpleNamespace(symbol=s, entry_ts=t, entry=p,"
        " stage1_atr7_pct=atr, episode_id=ep) for s,t,p,atr,ep"
        " in [('A_USDT',0,100.,4.5,1),('B_USDT',1800,200.,5.8,2)]], None, {'valid':True}\n"
        "def validate_exact_v2_dataset(anchors): return anchors == {'valid':True}\n"
        "def build_candidate_sets_exact_v2(episodes): return {'P15_A0_D0':episodes}\n"
    )
    atr_module = src / "atr_floor_v16.py"
    atr_module.write_text(
        "def build_floor_pools(cohort, *, floors=(4.,)): return {4.:cohort}\n"
    )
    candidates = [
        dict(symbol="A_USDT",entry_ts="0",entry_reference_price="100.0",
             stage1_atr7_pct="4.5",episode_id="1"),
        dict(symbol="B_USDT",entry_ts="1800",entry_reference_price="200.0",
             stage1_atr7_pct="5.8",episode_id="2"),
    ]
    archive = base / "v40.zip"
    with ZipFile(archive,"w") as z:
        z.writestr("manifest.json",json.dumps({
            "candidate_count":2,
            "stage_sha256":hashlib.sha256(stage.read_bytes()).hexdigest(),
            "type":"READ_ONLY_SOURCE_PARITY_SAMPLE",
            "no_exchange_keys_or_orders":True,
        }))
        z.writestr("candidates.csv",rows_to_csv(candidates))
    hashes = dict(
        FROZEN_ARCHIVE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        FROZEN_STAGE_CSV_SHA256=hashlib.sha256(stage.read_bytes()).hexdigest(),
        FROZEN_STAGE_MODULE_SHA256=hashlib.sha256(stage_module.read_bytes()).hexdigest(),
        FROZEN_ATR_MODULE_SHA256=hashlib.sha256(atr_module.read_bytes()).hexdigest(),
        EXPECTED_CANDIDATES=2,
    )
    return src,stage,archive,hashes


def test_original_pinned_source_regenerates_ordered_candidate_identity():
    with tempfile.TemporaryDirectory() as tmp:
        src,stage,archive,hashes = original_research_fixture(Path(tmp))
        with patch.multiple(audit,**hashes):
            report = audit.run(stage_csv=stage,archive=archive,source_dir=src)
        assert report["candidate_count"] == 2
        assert report["candidate_sequence_and_fields_identical"] is True
        assert report["original_stage_and_ATR4_module_hashes_verified"] is True
        assert report["independent_stage2_algorithm_certified"] is False
        assert report["live_authorization"] is False
        assert report["mexc_posts"] == 0


def test_stage_csv_tampering_fails_before_module_execution():
    with tempfile.TemporaryDirectory() as tmp:
        src,stage,archive,hashes = original_research_fixture(Path(tmp))
        stage.write_text("changed stage data")
        with patch.multiple(audit,**hashes):
            with pytest.raises(audit.FrozenCandidateSourceHalted,
                               match="PINNED_SOURCE_SHA256_MISMATCH") as info:
                audit.run(stage_csv=stage,archive=archive,source_dir=src)
            assert info.value.input_hash_matches == {
                "stage_csv":False, "archive":True,
                "stage_module":True, "atr_module":True,
            }


def test_module_tampering_fails_before_import():
    with tempfile.TemporaryDirectory() as tmp:
        src,stage,archive,hashes = original_research_fixture(Path(tmp))
        (src / "atr_floor_v16.py").write_text("raise Exception('unsafe')\n")
        with patch.multiple(audit,**hashes):
            with pytest.raises(audit.FrozenCandidateSourceHalted,
                               match="PINNED_SOURCE_SHA256_MISMATCH"):
                audit.run(stage_csv=stage,archive=archive,source_dir=src)


def test_candidate_changed_value_and_order_halt():
    a = [
        dict(symbol="A_USDT",entry_ts="0",entry_reference_price="100",
             stage1_atr7_pct="4.5",episode_id="1"),
        dict(symbol="B_USDT",entry_ts="1800",entry_reference_price="200",
             stage1_atr7_pct="5.8",episode_id="2"),
    ]
    assert audit.compare_candidate_rows(a,[dict(x) for x in a],expected_count=2)==2
    for corrupted in (
        [a[1],a[0]],
        [dict(a[0],entry_reference_price="101"),a[1]],
        [a[0],dict(a[1],episode_id="3")],
    ):
        with pytest.raises(audit.FrozenCandidateSourceHalted,
                           match="PINNED_ORIGINAL_CANDIDATE_REGENERATION_DRIFT"):
            audit.compare_candidate_rows(a,corrupted,expected_count=2)


def test_missing_or_duplicate_candidate_identity_rejected():
    base = dict(symbol="A_USDT",entry_ts="0",entry_reference_price="100",
                stage1_atr7_pct="4.5",episode_id="1")
    with pytest.raises(audit.FrozenCandidateSourceHalted,
                       match="CANDIDATE_IDENTITY_DUPLICATE"):
        audit.compare_candidate_rows([base,base],[base,base],expected_count=2)
    with pytest.raises(audit.FrozenCandidateSourceHalted,
                       match="CANDIDATE_ROW_COUNT_OR_FIELDS_INVALID"):
        audit.compare_candidate_rows([base],[base],expected_count=2)


def test_pinned_source_hashes_match_nov2_replay_provenance():
    # Catch transcription mistakes: the previously merged stage_reference
    # constant accidentally contained 67 hex characters, making all
    # legitimate original historical source checks fail closed.
    provenance = json.loads(
        (Path(__file__).resolve().parents[1] /
         "docs/evidence/issue-14/provenance.json").read_text()
    )
    original = provenance["original_module_hashes"]
    assert audit.FROZEN_ARCHIVE_SHA256 == provenance["frozen_archive_sha256"]
    assert audit.FROZEN_STAGE_MODULE_SHA256 == original["stage_reference.py"]
    assert audit.FROZEN_ATR_MODULE_SHA256 == original["atr_floor_v16.py"]
    for hash_ in (
        audit.FROZEN_ARCHIVE_SHA256,audit.FROZEN_STAGE_CSV_SHA256,
        audit.FROZEN_STAGE_MODULE_SHA256,audit.FROZEN_ATR_MODULE_SHA256,
    ):
        assert len(hash_) == 64
        assert set(hash_) <= set("0123456789abcdef")


def test_source_hash_diagnostic_marks_all_unverified_sources_without_printing_values():
    with tempfile.TemporaryDirectory() as tmp:
        src,stage,archive,hashes = original_research_fixture(Path(tmp))
        # Two independent mismatches must be visible in one invocation.
        stage.write_bytes(b"bad stage data")
        (src / "atr_floor_v16.py").write_bytes(b"bad source module")
        with patch.multiple(audit,**hashes):
            with pytest.raises(audit.FrozenCandidateSourceHalted,
                               match="PINNED_SOURCE_SHA256_MISMATCH") as info:
                audit.run(stage_csv=stage,archive=archive,source_dir=src)
        assert info.value.input_hash_matches == {
            "stage_csv":False,"archive":True,
            "stage_module":True,"atr_module":False,
        }
