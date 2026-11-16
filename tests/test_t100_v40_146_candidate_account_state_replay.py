"""Actual reconstructed account-state candidate decisions (prescribed fills, offline)."""
import csv
import importlib.util
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


AUDIT_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/issue-14/rebuild_marked_equity_drawdown.py"
)
spec=importlib.util.spec_from_file_location("v40_candidate_marked_replay",AUDIT_PATH)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BAR=1800
T=int(datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp())
SYM="TEST_USDT"


def write_csv(path,rows):
    with path.open("w",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fixture(root):
    bars=[(T-(49-i)*BAR,11.,10.,2000.) for i in range(49)]
    database=root/"candles.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE candles(symbol TEXT,interval TEXT,open_ts INTEGER,"
                   "open REAL,high REAL,close REAL,amount REAL)")
        db.executemany("INSERT INTO candles VALUES(?,?,?,?,?,?,?)",[
            (SYM,"Min30",when,10.,hi,cl,amt)
            for when,hi,cl,amt in bars
        ]+[
            (SYM,"Min30",T,10.,11.,10.,2000.),
            (SYM,"Min30",T+BAR,10.,12.,8.5,2000.),
            (SYM,"Min30",T+2*BAR,9.,10.,1.,2000.),
        ])
    funding=root/"funding.sqlite"
    with sqlite3.connect(funding) as db:
        db.execute("CREATE TABLE funding(symbol TEXT,settle_ts INTEGER,rate REAL)")
        db.executemany("INSERT INTO funding VALUES(?,?,?)",[
            (SYM,T,0.9),(SYM,T+2*BAR,0.01),
        ])
    rows={name:[] for name in ("entries","exits","fills","metrics")}
    for cost,pnl,final in ((100,"9.028","2008.798"),
                           (200,"8.128","2006.898")):
        base={"scenario":f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"}
        rows["entries"].append(dict(base,symbol=SYM,time=datetime.fromtimestamp(
            T,timezone.utc).isoformat(),notional="100",equity_at_entry="2000",
            atr="6",desired_before_liquidity="1250"))
        rows["exits"].append(dict(base,symbol=SYM,
            entry_time=datetime.fromtimestamp(T,timezone.utc).isoformat(),
            exit_time=datetime.fromtimestamp(T+2*BAR,timezone.utc).isoformat(),
            notional="100",pnl_net_before_funding=pnl,kind="normal_exit",exit="9"))
        rows["fills"].append(dict(base,symbol=SYM,
            time=datetime.fromtimestamp(T,timezone.utc).isoformat(),kind="entry",price="10"))
        rows["metrics"].append(dict(base,funding_net="0.85",funding_events="1",
            final_equity=final,high_envelope_dd_pct="0",close_dd_pct="0"))
    for name,data in rows.items():
        write_csv(root/(name+".csv"),data)
    return database,funding,bars


def candidate_source(bars, *, action="ADMIT", symbol=SYM):
    row={"symbol":symbol,"stage1_atr7_pct":"6"}
    gate={(SYM,T):action} if action is not None else {}
    return {T:[row]},{100:gate,200:gate},{(SYM,T):bars}


def run_case(root,databases,bars, *, action="ADMIT", alter_source=False):
    database,funding=databases
    if alter_source:
        bars=[(i,hi+1 if n==10 else hi,cl,amt)
              for n,(i,hi,cl,amt) in enumerate(bars)]
    with patch.object(module,"_pinned_candidate_data",
                      return_value=candidate_source(bars,action=action)),patch.multiple(
        module,
        start=T,end=T+2*BAR,
        EXPECTED_HISTORICAL_CANDIDATES=1,
        EXPECTED_HISTORICAL_GATE_SKIPS=0,
        EXPECTED_HISTORICAL_OCCUPANCY_REJECTS=0,
        EXPECTED_HISTORICAL_ADMISSIONS=1,
    ):
        return module.audit(root,{"historical":root},T,T+2*BAR,entry_specs={},
            candles_path=database,funding_path=funding,
            candidate_archive=root/"synthetic-archive.zip",immutable_candles=True)


def test_accepted_stage2_replayed_with_reconstructed_equity_and_actual_fills():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        database,funding,bars=fixture(root)
        report=run_case(root,(database,funding),bars)
        for cost in (100,200):
            row=report[f"historical_{cost}"]
            assert row["candidate_decisions_checked"]==1
            assert row["candidate_admissions_and_sizes_checked"]==1
            assert row["reconstructed_entry_sizes_checked"]==1
            assert row["candidate_decisions_given_prescribed_fills_verified"] is True
            assert row["full_strategy_decisions_independently_certified"] is False
            assert row["historical_point_in_time_specifications_certified"] is False
            assert row["live_authorization"] is False


def test_candidate_gate_drift_fails_before_executed_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        database,funding,bars=fixture(root)
        with pytest.raises(ValueError,match="CANDIDATE_VOL30_POLICY_DRIFT"):
            run_case(root,(database,funding),bars,action="SKIP")


def test_candidate_omission_cannot_hide_admissible_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        database,funding,bars=fixture(root)
        with pytest.raises(ValueError,match="CANDIDATE_OMISSION_POLICY_DRIFT"):
            run_case(root,(database,funding),bars,action=None)


def test_tampered_prior_candles_are_detected_before_admission():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        database,funding,bars=fixture(root)
        with pytest.raises(ValueError,match="CANDIDATE_CAUSAL_HISTORY_MISMATCH"):
            run_case(root,(database,funding),bars,alter_source=True)


def test_live_input_requires_complete_historical_window_and_specification_scope():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        with pytest.raises(ValueError,match="CANDIDATE_REPLAY_REQUIRES_COMPLETE_FROZEN_HISTORICAL_PATH"):
            module.audit(root,{"historical":root},candidate_archive=root/"file.zip")
