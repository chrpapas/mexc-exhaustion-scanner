"""Packaged real V40 historical-entry parity cannot be spoofed by code."""
import json
from types import SimpleNamespace

import pytest

from app.t100_live_strategy_parity_gate import (
    assess_v40_first_canary_entry_parity_report,
)
import app.t100_v40_first_canary_parity_evidence as m


def test_original_october_2026_audit_is_packaged_and_not_autonomous_claim():
    report=m.load_verified_frozen_v40_entry_parity()
    check=assess_v40_first_canary_entry_parity_report(report)
    assert check["first_canary_entry_parity_verified"] is True
    assert check["full_autonomous_mode_certified"] is False
    assert check["entry_allowed"] is False
    assert check["live_authorization"] is False
    assert report["discrepancies"]==0
    assert report["candidates"]==146
    assert report["prior_candles"]==7154
    assert report["reference_entry_notionals_exact"]==648
    assert report["archive_manifest"]["no_exchange_keys_or_orders"] is True
    assert any("No live order" in s for s in report["limitations"])


@pytest.mark.parametrize("change",[
    {"archive_sha256":"f"*64},
    {"status":"FAKE_ENTRY_PARITY"},
    {"candidates":147},
    {"discrepancies":1},
    {"limitations":[]},
])
def test_tampered_artifact_halts_instead_of_inventing_a_report(monkeypatch,change):
    original=m.load_verified_frozen_v40_entry_parity()
    payload={**original,**change}
    def fake_files(pkg):
        assert pkg=="app"
        class Loader:
            def joinpath(self,name):
                assert name==m._DOCUMENT
                return self
            def read_text(self,encoding):
                return json.dumps(payload)
        return Loader()
    monkeypatch.setattr(m,"files",fake_files)
    with pytest.raises(m.V40EntryParityEvidenceHalted,match="ORIGINAL_FROZEN"):
        m.load_verified_frozen_v40_entry_parity()
