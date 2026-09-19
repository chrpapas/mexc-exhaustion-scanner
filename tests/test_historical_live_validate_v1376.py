from __future__ import annotations

import sqlite3
from pathlib import Path

from app.historical_live_validate import validate_ticker_store


def mk(path: Path, rows):
    db=sqlite3.connect(path)
    db.execute("""CREATE TABLE ticker_snapshots(
      symbol TEXT, observed_at TEXT, last_price REAL, amount24 REAL,
      rise_fall_rate REAL, spread_pct REAL
    )""")
    db.executemany("INSERT INTO ticker_snapshots VALUES (?,?,?,?,?,?)", rows)
    db.commit(); db.close()


def test_validation_passes_identical_stores(tmp_path: Path):
    rows=[("A_USDT","2026-01-01 00:00:00+00",100,4_000_000,0.1,0.02)]
    a=tmp_path/"a.sqlite"; b=tmp_path/"b.sqlite"
    mk(a,rows); mk(b,rows)
    result=validate_ticker_store(a,b)
    assert result.gate_pass
    assert result.risk_agreement == 1.0


def test_validation_fails_missing_replay(tmp_path: Path):
    a=tmp_path/"a.sqlite"; b=tmp_path/"b.sqlite"
    mk(a,[("A_USDT","2026-01-01 00:00:00+00",100,4_000_000,0.1,0.02)])
    mk(b,[])
    result=validate_ticker_store(a,b)
    assert not result.gate_pass
