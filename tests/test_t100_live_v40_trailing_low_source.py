"""Prove trailing low never includes pre-entry or unfinished candles."""
from __future__ import annotations

import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D

import pytest

from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_trailing_low_source import (
    PostgresV40TrailingLowReader, V40TrailingLowHalted,
    inspect_completed_causal_lows, LOW_BARS_SQL,
)

UTC=timezone.utc
T=datetime(2026,11,8,18,0,0,tzinfo=UTC)
SYM="EXAMPLE_USDT"


def bar(ts,*,low=90.):
    return {
        "open_time":ts,
        "open":100.,"high":101.,
        "low":low,"close":99.,
    }


def test_midbar_entry_excludes_earlier_favorable_low():
    entry=T+timedelta(seconds=30)
    now=T+timedelta(minutes=92)
    # Completed bars at 18:30, 19:00; full earlier 18:00 bar may
    # contain a low that occurred before the actual 18:00:30 fill.
    res=inspect_completed_causal_lows(
        symbol=SYM,filled_at=entry,observed_at=now,
        rows=[bar(T+timedelta(minutes=30),low=97.),
              bar(T+timedelta(minutes=60),low=95.)],
    )
    assert res.complete_bars==2
    assert res.observed_low==D("95.0")
    assert res.first_completed_open_ts==int((T+timedelta(minutes=30)).timestamp())
    assert res.covers_since_entry_fill is False
    assert res.stop_amendment_authorized is False


def test_exact_bar_open_entry_allows_only_completed_bars():
    out=inspect_completed_causal_lows(
        symbol=SYM,filled_at=T,
        observed_at=T+timedelta(minutes=61),
        rows=[bar(T,low=96.),bar(T+timedelta(minutes=30),low=93.)],
    )
    assert out.complete_bars==2
    assert out.observed_low==D("93.0")
    assert not out.signed_fill_provenance_verified


def test_current_incomplete_bar_cannot_contribute_lookahead_low():
    with pytest.raises(V40TrailingLowHalted,match="FUTURE_OR_UNCOMPLETED"):
        inspect_completed_causal_lows(
            symbol=SYM,filled_at=T+timedelta(seconds=30),
            observed_at=T+timedelta(minutes=25),
            rows=[bar(T,low=10.)],
        )


def test_empty_history_is_non_authorizing_not_zero_low():
    out=inspect_completed_causal_lows(
        symbol=SYM,filled_at=T+timedelta(seconds=10),
        observed_at=T+timedelta(minutes=25),
        rows=[],
    )
    assert out.status=="V40_NO_CAUSAL_COMPLETED_BARS_NONAUTHORIZING"
    assert out.observed_low is None


def test_gaps_and_duplicates_fail_closed():
    entry=T
    now=T+timedelta(minutes=91)
    for rows in (
        [bar(T),bar(T+timedelta(minutes=60)),bar(T+timedelta(minutes=60))],
        [bar(T),bar(T+timedelta(minutes=30))],
    ):
        with pytest.raises(V40TrailingLowHalted):
            inspect_completed_causal_lows(
                symbol=SYM,filled_at=entry,
                observed_at=now,rows=rows,
            )


def test_malformed_ohlc_and_timezones_halt():
    with pytest.raises(V40TrailingLowHalted,match="OHLC"):
        inspect_completed_causal_lows(
            symbol=SYM,filled_at=T,
            observed_at=T+timedelta(minutes=31),
            rows=[bar(T,low=105.)],
        )
    with pytest.raises(V40TrailingLowHalted,match="NAIVE"):
        inspect_completed_causal_lows(
            symbol=SYM,filled_at=T.replace(tzinfo=None),
            observed_at=T+timedelta(minutes=31),
            rows=[],
        )


class Tx:
    async def __aenter__(self):return self
    async def __aexit__(self,*_):return False


class Conn:
    def __init__(self,rows,*,valid_role=True,fail=False):
        self.rows=rows
        self.valid_role=valid_role
        self.fail=fail
        self.calls=[]
    def transaction(self,*,isolation,readonly):
        assert isolation=="repeatable_read" and readonly is True
        return Tx()
    async def fetchval(self,sql):
        self.calls.append(sql)
        if "transaction_read_only" in sql:return "on"
        if "transaction_isolation" in sql:return "repeatable read"
        if "current_database" in sql:return "mexc_exhaustion"
        if "current_user" in sql:
            return TARGET_ROLE if self.valid_role else "mexc_scanner"
        raise AssertionError("unexpected read-only SQL")
    async def fetch(self,sql,*args):
        self.calls.append(sql)
        assert sql==LOW_BARS_SQL
        if self.fail:raise RuntimeError("secret DSN must not be logged")
        return self.rows


class Acquire:
    def __init__(self,c):self.c=c
    async def __aenter__(self):return self.c
    async def __aexit__(self,*_):return False


class Pool:
    def __init__(self,c):self.c=c
    def acquire(self):return Acquire(self.c)


def run_reader(c):
    return asyncio.run(PostgresV40TrailingLowReader(Pool(c)).read(
        symbol=SYM,filled_at=T+timedelta(seconds=10),
        observed_at=T+timedelta(minutes=91),
    ))


def test_source_reads_only_and_returns_conservative_low():
    c=Conn([
        bar(T+timedelta(minutes=30),low=90.),
        bar(T+timedelta(minutes=60),low=80.),
    ])
    result=run_reader(c)
    assert result.observed_low==D("80.0")
    assert not result.stop_amendment_authorized
    assert not any(sql.lstrip().upper().startswith(
        ("INSERT","UPDATE","DELETE","ALTER")
    ) for sql in c.calls)


def test_source_role_cannot_fall_back_to_privileged_scanner_account():
    with pytest.raises(V40TrailingLowHalted,match="RESTRICTED_READONLY"):
        run_reader(Conn([],valid_role=False))


def test_source_db_exception_redacted():
    with pytest.raises(V40TrailingLowHalted) as e:
        run_reader(Conn([],fail=True))
    assert str(e.value)=="V40_TRAILING_READONLY_POSTGRES_QUERY_FAILED"
    assert "secret" not in str(e.value)


def test_true_bar_low_from_before_fill_does_not_arm_trail():
    # This is the test that prevents a 14%-arm on a low that may
    # have occurred before the short was opened.
    entry=T+timedelta(minutes=1)
    out=inspect_completed_causal_lows(
        symbol=SYM,filled_at=entry,
        observed_at=T+timedelta(minutes=61),
        rows=[bar(T+timedelta(minutes=30),low=99.)],
    )
    assert out.observed_low==D("99.0")
    assert out.observed_low>D("86")
