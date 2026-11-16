"""Read-only V40 independent exit-quantity diagnostics; synthetic candles only."""
import json
import sqlite3
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pytest

from scripts.v40_independent_exit_quantity_model import (
    ExitQuantityAuditHalted, Lot, allocate, generate_position,
    compare_generated, open_offline_db, quote_capacity, audit
)


def test_partial_trigger_and_next_bar_delayed_full_close():
    result=generate_position(
        entry_ts=0,entry_price="100",entry_notional="100",
        entry_quote="100",
        candle_rows=[
            (0,100,180,90,110,4000), # 5% of strictly prior candle = 100
            (1800,110,120,95,105,2000),
        ],
    )
    events=result["events"]
    assert result["triggers"]==1 and not result["pending"]
    assert len(events)==2
    assert [e["kind"] for e in events]==["normal_exit","delayed_exit"]
    assert [e["price"] for e in events]==[D(175),D(110)]
    assert events[0]["reason"]=="pretrail_stop_partial"
    assert events[1]["reason"]=="delayed_forced_exit"
    assert abs(events[0]["notional"]-D(100)*D(100)/D(175))<D("1e-12")
    assert abs(sum(e["notional"] for e in events)-D(100))<D("1e-12")


def test_trailing_arm_requires_prior_closed_candle():
    result=generate_position(
        entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
        candle_rows=[
            (0,100,100,80,90,10000),
            (1800,85,90,84,87,10000),
        ],
    )
    assert len(result["events"])==1
    assert result["events"][0]["time"]==1800
    assert result["events"][0]["price"]==D(85)
    assert result["events"][0]["reason"]=="trail"


def test_known_lot_exit_partial_and_delayed_quantity_are_independent():
    lot=Lot(D(".01"),D(1),D(1))
    result=generate_position(
        entry_ts=0,entry_price=100,entry_notional=100,
        entry_quote=100,lot=lot,entry_units=D(100),
        candle_rows=[
            (0,100,180,90,110,4000),
            (1800,110,120,95,105,2000),
        ],
    )
    events=result["events"]
    assert [e["notional"] for e in events]==[D(57),D(43)]
    assert [e["quote"] for e in events]==[D("99.75"),D("47.3")]
    assert not result["pending"]


def test_minimum_lot_cannot_fill_tiny_quote_quota_and_delays():
    lot=Lot(D(1),D(1),D(1))
    result=generate_position(
        entry_ts=0,entry_price=100,entry_notional=100,
        entry_quote=100,lot=lot,entry_units=D(1),
        candle_rows=[
            (0,100,180,90,110,2100), # 105 quota less 100 entry = 5 left; not one contract
            (1800,110,120,95,105,5000),
        ],
    )
    assert len(result["events"])==1
    assert result["events"][0]["time"]==1800
    assert result["events"][0]["kind"]=="delayed_exit"
    assert result["events"][0]["notional"]==D(100)


def test_event_comparison_detects_quantities_prices_and_missing_delays():
    modeled=generate_position(
        entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
        candle_rows=[(0,100,180,90,110,4000),(1800,110,120,95,105,2000)],
    )["events"]
    def observed(rows):
        return [{"exit_time":f"1970-01-01T00:{i:02d}:00+00:00",
                 "kind":e["kind"],"exit":str(e["price"]),
                 "notional":str(e["notional"]),"reason":e["reason"]}
                for i,e in rows]
    actual=observed([(0,modeled[0]),(30,modeled[1])])
    perfect=compare_generated(modeled,actual)
    assert perfect["event_count_mismatch"]==0
    assert perfect["quantity_mismatches"]==0
    changed=[dict(x) for x in actual]
    changed[0]["notional"]="10"
    assert compare_generated(modeled,changed)["quantity_mismatches"]==1
    changed=[dict(x) for x in actual]
    changed[0]["exit"]="174"
    assert compare_generated(modeled,changed)["price_mismatches"]==1
    assert compare_generated(modeled,actual[:1])["event_count_mismatch"]==1


def test_missing_or_contiguous_candles_and_unsafe_entry_lots_halt():
    with pytest.raises(ExitQuantityAuditHalted,match="ENTRY_CANDLE_MISSING"):
        generate_position(entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
                          candle_rows=[(1800,100,180,90,110,2000)])
    with pytest.raises(ExitQuantityAuditHalted,match="HISTORICAL_CANDLE_GAP"):
        generate_position(entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
                          candle_rows=[(0,100,100,90,90,2000),
                                       (3600,100,180,90,110,2000)])
    with pytest.raises(ExitQuantityAuditHalted,match="ENTRY_LOT_NOTIONAL_DRIFT"):
        generate_position(entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
                          lot=Lot(D(1),D(1),D(1)),entry_units=D(2),
                          candle_rows=[(0,100,180,90,110,4000)])


def test_sqlite_immutable_source_refuses_pending_wal_and_writes():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/"offline-candles.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE candles (symbol TEXT,open_ts INTEGER)")
        with open_offline_db(path,immutable=True) as db:
            assert db.execute("PRAGMA query_only").fetchone()[0]==1
            with pytest.raises(sqlite3.DatabaseError):
                db.execute("INSERT INTO candles VALUES('x',0)")
        for suffix in ("-wal","-journal"):
            side=Path(str(path)+suffix)
            side.write_bytes(b"pending")
            try:
                with pytest.raises(ExitQuantityAuditHalted,match="PENDING_JOURNAL"):
                    open_offline_db(path,immutable=True)
            finally:
                side.unlink()


def test_current_lot_reference_required_for_dataset_audit():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        bad=root/"specs.json"
        bad.write_text(json.dumps({"specifications":{},"NOT_HISTORICAL":False}))
        with pytest.raises(ExitQuantityAuditHalted,match="SNAPSHOT_PROVENANCE_UNVERIFIED"):
            audit(root,root/"irrelevant.sqlite",bad,immutable=True)


def test_entry_consumes_same_bar_quota_before_stop():
    result=generate_position(
        entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
        candle_rows=[
            (0,100,180,90,110,2000), # exactly 100 quota spent by entry
            (1800,110,120,95,105,5000),
        ],
    )
    assert result["triggers"]==1
    assert len(result["events"])==1
    assert result["events"][0]["kind"]=="delayed_exit"
    assert result["events"][0]["time"]==1800
    assert result["events"][0]["notional"]==D(100)


def test_entry_cannot_use_more_than_prior_bar_total_quota():
    with pytest.raises(ExitQuantityAuditHalted,match="ENTRY_EXCEEDS_PRIOR30_QUOTA"):
        generate_position(
            entry_ts=0,entry_price=100,entry_notional=100,entry_quote=100,
            candle_rows=[(0,100,180,90,110,100)],
        )
