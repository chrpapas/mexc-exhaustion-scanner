from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.historical_research import (
    CacheLock,
    HistoricalFetchConfig,
    _atomic_gzip_json,
    _chunk_ranges,
    _is_valid_cached_chunk,
    audit_cache,
)


def _config(tmp_path: Path, **overrides):
    values = dict(
        cache_dir=tmp_path,
        start=datetime(2026, 3, 1, tzinfo=UTC),
        end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    values.update(overrides)
    return HistoricalFetchConfig(**values)


def test_historical_research_rate_is_deliberately_hard_capped(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, requests_per_second=2.01).validate()


def test_chunk_ranges_are_resumable_and_non_overlapping():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    rows = list(_chunk_ranges(start, end, "Min15", 1200))
    assert len(rows) >= 3
    for (a, b), (c, _d) in zip(rows, rows[1:]):
        assert c > b
        assert c - b == timedelta(minutes=15)


def test_atomic_chunk_validation_and_audit(tmp_path):
    path = tmp_path / "candles" / "Min15" / "TEST_USDT" / "1-2.json.gz"
    payload = {"schema": 1, "data": {"time": [1, 2], "open": [1, 1]}}
    _atomic_gzip_json(path, payload)
    assert _is_valid_cached_chunk(path)
    report = audit_cache(tmp_path)
    assert report["valid_chunks"] == 1
    assert report["candles"] == 2
    assert report["symbols"] == 1


def test_cache_lock_prevents_second_collector(tmp_path):
    with CacheLock(tmp_path):
        with pytest.raises(RuntimeError):
            with CacheLock(tmp_path):
                pass


def test_module_has_no_database_dependency():
    import app.historical_research as module
    source = Path(module.__file__).read_text()
    assert "from app.db" not in source
    assert "from app.trader_db" not in source
    assert "DATABASE_URL" not in source
