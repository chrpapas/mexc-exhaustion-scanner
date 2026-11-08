"""Offline test matrix for strict read-only V40 Stage2 live signal intake."""
from __future__ import annotations

import asyncio
from datetime import datetime,timedelta,timezone
from copy import deepcopy

import pytest

from app.t100_200_strategy import STRATEGY_ID
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import (
    HISTORY_SQL,SIGNALS_SQL,MAX_SIGNAL_AGE_SECONDS,MAX_BATCH,
    PostgresV40Stage2Source,V40SignalIntakeHalted,
    inspect_stage2_candidate,
)

UTC=timezone.utc
T=datetime(2026,11,8,18,0,0,tzinfo=UTC)


def fixtures():
    signal={
        "id":42,"strategy_id":STRATEGY_ID,
        "symbol":"EXAMPLE_USDT","p2_at":T,
        "feature_at":T-timedelta(minutes=30),
        "created_at":T+timedelta(seconds=8),
        "entry_price":100.,"physical_episode_seq":2,
        "stage_no":2,"stage1_return_24h":.20,
        "stage1_atr7_pct":5.8,
        "stage1_strict365":True,"stage1_eligible":True,
        "t100_tier":"HIGH_225","eligible_stage2":True,
        # Paper's own synthetic trader may already have consumed a signal.
        "trade_decision":"accepted",
    }
    bars=[
        {
            "open_time":T-timedelta(minutes=30*i),
            "high":102.,"close":100.,"amount":100000.
        } for i in range(1,50)
    ]
    return signal,bars


def test_compatible_with_paper_consumed_signal_and_49_bar_admission():
    sig,history=fixtures()
    candidate=inspect_stage2_candidate(
        sig,history,observed_at=T+timedelta(seconds=25)
    )
    assert candidate.signal_id==42
    assert candidate.strategy_id==STRATEGY_ID
    assert candidate.symbol=="EXAMPLE_USDT"
    assert candidate.tier=="HIGH_225"
    assert candidate.pre24_vol_pct==0
    assert len(candidate.prior_bars)==49
    assert candidate.prior_bars[-1].open_ts==int(
        (T-timedelta(minutes=30)).timestamp()
    )
    assert candidate.observed_age_seconds==25
    assert not candidate.entry_allowed
    assert not candidate.live_authorization


@pytest.mark.parametrize("kind",[
    "bad_strategy","future","stale","wrong_feature_time","bad_stage",
    "bad_tier","invalid_stage1","wrong_return","bad_atr",
    "bad_reference","bad_symbol","bad_episode","late_creation",
])
def test_signal_contract_failure_halts(kind):
    sig,history=fixtures()
    clock=T+timedelta(seconds=25)
    if kind=="bad_strategy":
        sig["strategy_id"]="t100_broken_contract"
    if kind=="future":
        clock=T-timedelta(seconds=3)
    if kind=="stale":
        clock=T+timedelta(seconds=901)
    if kind=="wrong_feature_time":
        sig["feature_at"]=T
    if kind=="bad_stage":
        sig["stage_no"]=3
    if kind=="bad_tier":
        sig["t100_tier"]="LOW_100"
    if kind=="invalid_stage1":
        sig["stage1_eligible"]=False
    if kind=="wrong_return":
        sig["stage1_return_24h"]=.1
    if kind=="bad_atr":
        sig["stage1_atr7_pct"]=3.8
    if kind=="bad_reference":
        sig["entry_price"]=99.
    if kind=="bad_symbol":
        sig["symbol"]="BTC_USDT"
    if kind=="bad_episode":
        sig["physical_episode_seq"]=0
    if kind=="late_creation":
        sig["created_at"]=T+timedelta(minutes=2)
    with pytest.raises(V40SignalIntakeHalted):
        inspect_stage2_candidate(sig,history,observed_at=clock)


def test_missing_oldest_candle_cannot_be_filled_in():
    sig,history=fixtures()
    history=history[:-1]
    with pytest.raises(V40SignalIntakeHalted,match="49_BAR_COUNT"):
        inspect_stage2_candidate(sig,history,observed_at=T+timedelta(seconds=25))


def test_duplicate_or_gap_in_prior_bars_is_not_causal():
    sig,history=fixtures()
    history[14]["open_time"]=history[13]["open_time"]
    with pytest.raises(V40SignalIntakeHalted,match="DUPLICATE"):
        inspect_stage2_candidate(sig,history,observed_at=T+timedelta(seconds=25))
    sig,history=fixtures()
    history[14]["open_time"]-=timedelta(seconds=1)
    with pytest.raises(V40SignalIntakeHalted,match="49_BAR_TIMING"):
        inspect_stage2_candidate(sig,history,observed_at=T+timedelta(seconds=25))


def test_no_future_candle_allowed():
    sig,history=fixtures()
    history[0]["open_time"]=T
    with pytest.raises(V40SignalIntakeHalted):
        inspect_stage2_candidate(sig,history,observed_at=T+timedelta(seconds=25))


class Tx:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return False


class FakeConn:
    def __init__(self, signals, history, *, readonly_role=True,
                 fail_query=False):
        self.signals=signals
        self.history=history
        self.readonly_role=readonly_role
        self.fail_query=fail_query
        self.queries=[]
    def transaction(self, *, isolation, readonly):
        assert isolation=="repeatable_read" and readonly is True
        return Tx()
    async def fetchval(self, sql):
        self.queries.append(sql)
        if "transaction_read_only" in sql:
            return "on"
        if "transaction_isolation" in sql:
            return "repeatable read"
        if "current_database" in sql:
            return "mexc_exhaustion"
        if "current_user" in sql:
            return TARGET_ROLE if self.readonly_role else "mexc_scanner"
        raise AssertionError("unexpected SQL")
    async def fetch(self, sql,*args):
        self.queries.append(sql)
        if self.fail_query:
            raise RuntimeError("secret: DB host and token must not be shown")
        if sql == SIGNALS_SQL:
            assert args[2] <= MAX_BATCH+1
            return self.signals
        if sql == HISTORY_SQL:
            return self.history
        raise AssertionError("unexpected SQL")


class Acquire:
    def __init__(self,conn):
        self.conn=conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self,*_):
        return False


class Pool:
    def __init__(self,conn):
        self.conn=conn
    def acquire(self):
        return Acquire(self.conn)


def read(conn,**kw):
    return asyncio.run(PostgresV40Stage2Source(Pool(conn)).latest_candidates(
        observed_at=T+timedelta(seconds=25),**kw
    ))


def test_reader_single_readonly_snapshot_and_paper_decision_is_ignored():
    sig,history=fixtures()
    conn=FakeConn([sig],history)
    x=read(conn)
    assert x.status=="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
    assert len(x.candidates)==1
    assert x.candidates[0].signal_id==42
    assert x.read_only_repeatable_read
    assert not x.entry_allowed
    assert not x.live_authorization
    assert "trade_decision IS NULL" not in SIGNALS_SQL
    assert not any(
        sql.lstrip().upper().startswith(("INSERT","UPDATE","DELETE","ALTER"))
        for sql in conn.queries
    )


def test_reader_refuses_unbounded_batch_instead_of_silently_dropping():
    sig,history=fixtures()
    conn=FakeConn([sig,deepcopy(sig)],history)
    with pytest.raises(V40SignalIntakeHalted,match="TRUNCATION"):
        read(conn,limit=1)


def test_reader_refuses_same_signal_twice():
    sig,history=fixtures()
    conn=FakeConn([sig,deepcopy(sig)],history)
    with pytest.raises(V40SignalIntakeHalted,match="DUPLICATE"):
        read(conn,limit=2)


def test_restricted_runtime_role_is_mandatory_even_in_readonly_tx():
    sig,history=fixtures()
    conn=FakeConn([sig],history,readonly_role=False)
    with pytest.raises(V40SignalIntakeHalted,match="RESTRICTED"):
        read(conn)


def test_failed_database_read_hides_secrets():
    sig,history=fixtures()
    conn=FakeConn([sig],history,fail_query=True)
    with pytest.raises(V40SignalIntakeHalted) as error:
        read(conn)
    assert str(error.value)=="SOURCE_READ_ONLY_POSTGRES_QUERY_FAILED"
    assert "secret" not in str(error.value)


def test_reader_rejects_missing_timezone_or_excessive_age_window():
    conn=FakeConn([],[])
    async def call(**kwargs):
        return await PostgresV40Stage2Source(Pool(conn)).latest_candidates(
            **kwargs
        )
    with pytest.raises(V40SignalIntakeHalted):
        asyncio.run(call(observed_at=T.replace(tzinfo=None)))
    with pytest.raises(V40SignalIntakeHalted):
        asyncio.run(call(observed_at=T,max_age_seconds=901))


def test_valid_empty_batch_is_non_authorizing():
    conn=FakeConn([],[])
    x=read(conn)
    assert x.candidates == ()
    assert x.status=="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
