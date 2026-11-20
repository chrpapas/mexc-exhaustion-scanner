"""Offline source-attestation regression. No MEXC or SQL I/O."""
from hashlib import sha256
from io import StringIO
import csv
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

import app.t100_v40_frozen_replay_identity as frozen


def _csv(rows):
    buf=StringIO()
    fields=sorted({k for row in rows for k in row})
    writer=csv.DictWriter(buf,fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def archive(tmp_path:Path,**overrides):
    manifest=dict(candidate_count=146,preceding_candles_exported=7154,
                  no_exchange_keys_or_orders=True)
    scenario={**frozen.EXPECTED_METRICS,**overrides,
              "scenario":frozen.SCENARIO}
    names={"manifest.json","candidates.csv","prior49_bars.csv",
           *{f"capital_{c}/{f}.csv" for c in ("2000","10000","100000")
             for f in ("metrics","entries","gate_decisions")}}
    path=tmp_path/"fixture.zip"
    with ZipFile(path,"w") as z:
        for name in sorted(names):
            if name=="manifest.json":
                z.writestr(name,json.dumps(manifest))
            elif name=="capital_2000/metrics.csv":
                z.writestr(name,_csv([scenario]))
            elif name=="capital_2000/entries.csv":
                z.writestr(name,_csv([{"scenario":frozen.SCENARIO,
                                      "signal_id":str(i)} for i in range(108)]))
            elif name=="capital_2000/gate_decisions.csv":
                z.writestr(name,_csv([{"scenario":frozen.SCENARIO,
                                      "signal_id":str(i)} for i in range(132)]))
            else:
                z.writestr(name,"header\nplaceholder\n")
    return path


def test_pinned_original_scenario_and_simulation_risk_metrics():
    assert frozen.ZIP_SHA256==(
        "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
    )
    assert frozen.SCENARIO=="V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"
    record=frozen.V40FrozenIdentity()
    assert record.actual_peak_gross_to_equity>1
    assert record.modeled_margin_leverage_proxy==5
    assert record.entry_count==record.closed_count+record.open_at_horizon
    assert record.exit_partial_fills==23
    assert record.delayed_exits==16
    assert record.sl75_pct==75
    assert record.trail_arm_pct==14
    assert record.trail_gap_percentage_points==.5
    assert record.exchange_margin_and_liquidation_certified is False
    assert record.full_live_execution_authorized is False


def test_exact_scenario_fields_match_unmodified_fixture(tmp_path,monkeypatch):
    p=archive(tmp_path)
    monkeypatch.setattr(frozen,"ZIP_SHA256",sha256(p.read_bytes()).hexdigest())
    r=frozen.verify_original_v40_archive(p)
    assert r.status=="V40_ORIGINAL_FROZEN_ARCHIVE_MATCH_NONAUTHORIZING"
    assert r.exchange_posts==0
    assert r.open_at_horizon==3


@pytest.mark.parametrize("change",[
    {"peak_gross_to_equity":"1.0"},
    {"nominal_leverage":"1.0"},
    {"activation":"5.0"},
    {"open_positions":"0"},
    {"partial_exits":"0"},
    {"historical_venue_executable_certified":"True"},
    {"missing_spec_entries_passthrough":"0"},
])
def test_metrics_drifts_abort_even_with_valid_zip_hash(tmp_path,monkeypatch,change):
    p=archive(tmp_path,**change)
    monkeypatch.setattr(frozen,"ZIP_SHA256",sha256(p.read_bytes()).hexdigest())
    with pytest.raises(frozen.V40FrozenArchiveHalted,match="METRIC_DRIFT"):
        frozen.verify_original_v40_archive(p)


def test_corrupted_unpinned_archive_refused_before_parse(tmp_path):
    p=archive(tmp_path)
    with pytest.raises(frozen.V40FrozenArchiveHalted,match="HASH_MISMATCH"):
        frozen.verify_original_v40_archive(p)
