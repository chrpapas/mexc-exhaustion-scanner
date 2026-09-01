from __future__ import annotations

import inspect

from app.db import Database


def test_v1340_htf_backfill_is_bounded_and_keeps_candle_time_indexable():
    sig = inspect.signature(Database.backfill_research_htf_features)
    assert sig.parameters["batch_size"].default == 64
    assert sig.parameters["statement_timeout_seconds"].default == 20

    source = inspect.getsource(Database.backfill_research_htf_features)
    assert "c15.open_time <= f.confirmed_at - interval '15 minutes'" in source
    assert "c4.open_time <= f.confirmed_at - interval '4 hours'" in source
    assert "c15.open_time + interval '15 minutes'" not in source
    assert "c4.open_time + interval '4 hours'" not in source
    assert "set_config('statement_timeout'" in source
    assert "batch_size = max(1, min(int(batch_size), 128))" in source


def test_v1340_retry_ignores_permanent_cross_section_only_missing_rows():
    source = inspect.getsource(Database.backfill_research_htf_features)
    # retry_missing is only for fields that can actually be reconstructed from
    # stored ticker/candle history. Historical percentile is never fabricated.
    retry_fragment = source.split('retry_clause = ""', 1)[1].split('async with self.pool.acquire()', 1)[0]
    assert "'return_24h'" in retry_fragment
    assert "'distance_above_ema20_atr_4h'" in retry_fragment
    assert "'previous_momentum_1h'" in retry_fragment
    assert "'cross_section_percentile'" not in retry_fragment
    assert "retry_before" in retry_fragment


def test_v1340_one_shot_htf_backfill_command_exists():
    import app.research_htf_backfill_now as command

    source = inspect.getsource(command.main)
    assert "retry_missing=False" in source
    assert "retry_missing=True" in source
    assert "HTF backfill complete:" in source
    assert "computable=" in source
