"""Offline regression: historical Stage2 counts and exact scanner heartbeat.

All SQL reads under a forced READ ONLY, REPEATABLE READ, identity-pinned
owner transaction. This diagnostic is not an entry or scanner-health permit.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timedelta,timezone

import pytest

from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from app.t100_live_v40_stage2_source_health import (
    HEARTBEAT_SQL,RUNTIME_SQL,RECENT_ELIGIBLE_SQL,LAST_ELIGIBLE_SQL,
    inspect_scanner_source_health,
)

NOW=datetime(2026,11,11,8,20,tzinfo=timezone.utc)


class Tx:
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False


class Conn:
    def __init__(self):
        self.role="mexc_scanner"
        self.db="mexc_exhaustion"
        self.read_only="on"
        self.isolation="repeatable read"
        self.hb=NOW-timedelta(seconds=60)
        self.eval=NOW-timedelta(minutes=25)
        self.latest=NOW-timedelta(days=2)
        self.events=3
        self.queries=[]
        self.secret_error=False
    def transaction(self,*,isolation,readonly):
        assert isolation=="repeatable_read" and readonly is True
        return Tx()
    async def fetchval(self,sql,*args):
        self.queries.append((sql,args))
        if self.secret_error:raise RuntimeError("SENSITIVE_DB_PASSWORD")
        if sql=="SELECT current_setting('transaction_read_only')":
            return self.read_only
        if sql=="SELECT current_setting('transaction_isolation')":
            return self.isolation
        if sql=="SELECT current_database()":
            return self.db
        if sql=="SELECT current_user":
            return self.role
        if sql==RECENT_ELIGIBLE_SQL:
            assert len(args)==2
            assert args[0]==NOW-timedelta(days=7)
            assert args[1]==NOW
            return self.events
        raise AssertionError("UNEXPECTED_SQL")
    async def fetchrow(self,sql,*args):
        self.queries.append((sql,args))
        if self.secret_error:raise RuntimeError("SENSITIVE_DB_PASSWORD")
        if sql==HEARTBEAT_SQL:return (
            {"last_seen_at":self.hb} if self.hb is not None else None
        )
        if sql==RUNTIME_SQL:return {"last_eval_at":self.eval}
        if sql==LAST_ELIGIBLE_SQL:return (
            {"p2_at":self.latest} if self.latest is not None else None
        )
        raise AssertionError("UNEXPECTED_SQL")


class Acquire:
    def __init__(self,conn):self.conn=conn
    async def __aenter__(self):return self.conn
    async def __aexit__(self,*args):return False


class Pool:
    def __init__(self,conn):self.conn=conn
    def acquire(self):return Acquire(self.conn)


def read(c,*,manual=True):
    return asyncio.run(inspect_scanner_source_health(
        PostgresV40Stage2Source(
            Pool(c),manual_scanner_owner_readonly=manual,
        ),observed_at=NOW,
    ))


def test_recent_scanner_heartbeat_eval_and_historical_stage2_count_are_observations_only():
    c=Conn()
    result=read(c)
    assert result.status=="STAGE2_SCANNER_RECENT_HEARTBEAT_AND_EVALUATION_OBSERVED_NONAUTHORIZING"
    assert result.database_and_scanner_role_verified is True
    assert result.scanner_heartbeat_recent is True
    assert result.scanner_evaluation_recent is True
    assert result.eligible_stage2_events_last_7d==3
    assert result.latest_eligible_stage2_utc==c.latest.isoformat()
    assert result.scanner_heartbeat_utc==c.hb.isoformat()
    assert result.scanner_last_eval_utc==c.eval.isoformat()
    assert result.scanner_process_healthy_certified is False
    assert result.ingestion_completeness_certified is False
    assert result.live_signal_tradable is False
    assert result.live_authorization is False
    assert result.entry_allowed is False
    assert result.mexc_posts==0 and result.database_writes==0
    assert result.read_only
    assert len(c.queries)==8
    assert not any(q.strip().upper().startswith(
        ("INSERT","UPDATE","DELETE","DROP","CREATE","ALTER","GRANT")
    ) for q,_ in c.queries)


class AsyncpgLikeRecord:
    """Exercise asyncpg.Record-style dict(row), without Mapping inheritance."""
    def __init__(self,**data):
        self._data=data
    def keys(self):
        return self._data.keys()
    def get(self,key,default=None):
        return self._data.get(key,default)
    def __getitem__(self,key):
        return self._data[key]
    def __iter__(self):
        # asyncpg.Record iteration returns values, not mapping keys.
        return iter(self._data.values())


def test_actual_asyncpg_like_fetchrow_is_validated_without_mapping_subclass():
    from collections.abc import Mapping
    class RealShapeConn(Conn):
        async def fetchrow(self,sql,*args):
            raw=await super().fetchrow(sql,*args)
            return AsyncpgLikeRecord(**raw) if raw is not None else None
    assert not isinstance(AsyncpgLikeRecord(last_seen_at=NOW),Mapping)
    c=RealShapeConn()
    out=read(c)
    assert out.database_and_scanner_role_verified is True
    assert out.status=="STAGE2_SCANNER_RECENT_HEARTBEAT_AND_EVALUATION_OBSERVED_NONAUTHORIZING"
    assert out.scanner_heartbeat_recent and out.scanner_evaluation_recent
    assert out.eligible_stage2_events_last_7d==3
    assert out.latest_eligible_stage2_utc==c.latest.isoformat()
    assert out.entry_allowed is False and out.live_authorization is False
    assert out.mexc_posts==out.database_writes==0
    assert len(c.queries)==8


@pytest.mark.parametrize("changed_row",[
    AsyncpgLikeRecord(unexpected=NOW),
    AsyncpgLikeRecord(last_seen_at=NOW,extra=NOW),
    "2026-11-11T08:20:00+00:00",
    ["last_seen_at",NOW],
])
def test_health_readback_rejects_bad_real_record_shape(changed_row):
    class BadShapeConn(Conn):
        async def fetchrow(self,sql,*args):
            result=await super().fetchrow(sql,*args)
            return changed_row if sql==HEARTBEAT_SQL else result
    out=read(BadShapeConn())
    assert out.status=="STAGE2_SOURCE_HEALTH_READBACK_INVALID_HALT"
    assert out.database_and_scanner_role_verified is False
    assert out.eligible_stage2_events_last_7d is None
    assert out.entry_allowed is False and out.live_authorization is False


def test_zero_signals_in_seven_days_does_not_imply_scanner_stopped():
    c=Conn()
    c.events=0
    c.latest=None
    out=read(c)
    assert out.status.endswith("_OBSERVED_NONAUTHORIZING")
    assert out.eligible_stage2_events_last_7d==0
    assert out.latest_eligible_stage2_utc is None
    assert out.scanner_heartbeat_recent is True
    assert out.scanner_evaluation_recent is True
    assert not out.live_signal_tradable


@pytest.mark.parametrize("heartbeat_age,eval_age",[
    (601,100),(30,3601),(999999,999999),
])
def test_stale_scanner_activity_is_observed_not_called_live_safe(
    heartbeat_age,eval_age,
):
    c=Conn()
    c.hb=NOW-timedelta(seconds=heartbeat_age)
    c.eval=NOW-timedelta(seconds=eval_age)
    out=read(c)
    assert out.status=="STAGE2_SCANNER_HEARTBEAT_OR_EVALUATION_NOT_RECENT_NONAUTHORIZING"
    assert not (out.scanner_heartbeat_recent and out.scanner_evaluation_recent)
    assert out.database_and_scanner_role_verified
    assert not out.live_authorization


def test_missing_heartbeat_and_runtime_are_not_a_false_pass():
    c=Conn()
    c.hb=None
    c.eval=None
    out=read(c)
    assert not out.scanner_heartbeat_recent
    assert not out.scanner_evaluation_recent
    assert out.status=="STAGE2_SCANNER_HEARTBEAT_OR_EVALUATION_NOT_RECENT_NONAUTHORIZING"


@pytest.mark.parametrize("field,value",[
    ("read_only","off"),("isolation","read committed"),
    ("db","other_database"),("role","t100_live_executor_runtime"),
    ("role","t100_shadow_witness_writer"),
])
def test_bad_database_or_role_refuses_before_any_activity_query(field,value):
    c=Conn()
    setattr(c,field,value)
    out=read(c)
    assert out.status=="STAGE2_SOURCE_HEALTH_READONLY_IDENTITY_UNVERIFIED_HALT"
    assert out.database_and_scanner_role_verified is False
    assert len(c.queries)<=4
    assert out.database_writes==0


def test_owner_mode_is_explicit_and_default_production_contract_unmodified():
    c=Conn()
    out=read(c,manual=False)
    assert out.status=="STAGE2_SOURCE_HEALTH_OWNER_READONLY_REQUIRED_HALT"
    assert c.queries==[]


def test_secret_db_error_redacted_and_denies_certification():
    c=Conn()
    c.secret_error=True
    out=read(c)
    assert out.status=="STAGE2_SOURCE_HEALTH_READONLY_INSPECTION_FAILED_HALT"
    assert "PASSWORD" not in str(out)
    assert out.scanner_process_healthy_certified is False


def test_future_or_malformed_times_cannot_turn_into_recent_heartbeat():
    c=Conn()
    c.hb=NOW+timedelta(hours=1)
    out=read(c)
    assert out.status=="STAGE2_SOURCE_HEALTH_READONLY_INSPECTION_FAILED_HALT"
    c=Conn()
    c.eval="2026-11-11 08:00:00"
    out=read(c)
    assert out.status=="STAGE2_SOURCE_HEALTH_READONLY_INSPECTION_FAILED_HALT"


def test_negative_or_boolean_event_counts_are_not_accepted():
    for count in (-1,True,1.5):
        c=Conn()
        c.events=count
        out=read(c)
        assert out.status=="STAGE2_SOURCE_HEALTH_READBACK_INVALID_HALT"
        assert out.eligible_stage2_events_last_7d is None
