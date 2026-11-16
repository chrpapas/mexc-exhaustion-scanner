"""Synthetic provenance-manifest tests; no original local code is executed."""
import hashlib
import json
from pathlib import Path
import tempfile

import pytest

from scripts import v40_original_module_manifest_audit as a


def fixture(root):
    source=root/"research"
    source.mkdir()
    modules={}
    for i in range(a.EXPECTED_MODULES):
        name=f"module_{i}.py"
        content=f"# research source {i}\n"
        (source/name).write_text(content)
        modules[name]=hashlib.sha256(content.encode()).hexdigest()
    p=root/"provenance.json"
    p.write_text(json.dumps({
        "frozen_archive_sha256":"2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5",
        "original_module_hashes":modules,
    }))
    return source,p


def test_full_manifest_matches_without_execution():
    with tempfile.TemporaryDirectory() as tmp:
        src,p=fixture(Path(tmp))
        report=a.audit(src,p)
        assert report["status"]=="V40_ORIGINAL_SOURCE_MODULE_MANIFEST_MATCH"
        assert report["verified_module_count"]==a.EXPECTED_MODULES
        assert report["transitive_runtime_dependency_closure_certified"] is False
        assert report["live_authorization"] is False


def test_source_mismatch_and_missing_are_separate():
    with tempfile.TemporaryDirectory() as tmp:
        src,p=fixture(Path(tmp))
        (src/"module_0.py").write_text("modified")
        (src/"module_1.py").unlink()
        report=a.audit(src,p)
        assert report["status"]=="V40_ORIGINAL_SOURCE_MODULE_MANIFEST_INCOMPLETE"
        assert report["mismatched_module_names"]==["module_0.py"]
        assert report["missing_module_names"]==["module_1.py"]
        assert report["verified_module_count"]==a.EXPECTED_MODULES-2


def test_provenance_path_traversal_and_missing_hash_prevent_audit():
    with tempfile.TemporaryDirectory() as tmp:
        src,p=fixture(Path(tmp))
        report=json.loads(p.read_text())
        report["original_module_hashes"]["../outside.py"]="a"*64
        p.write_text(json.dumps(report))
        with pytest.raises(a.ModuleManifestHalted,match="PROVENANCE_PIN_SET_INVALID"):
            a.audit(src,p)


def test_wrong_archive_pin_cannot_certify_modules():
    with tempfile.TemporaryDirectory() as tmp:
        src,p=fixture(Path(tmp))
        report=json.loads(p.read_text())
        report["frozen_archive_sha256"]="0"*64
        p.write_text(json.dumps(report))
        with pytest.raises(a.ModuleManifestHalted,match="PROVENANCE_PIN_SET_INVALID"):
            a.audit(src,p)
