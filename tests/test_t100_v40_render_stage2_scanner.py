"""No-network/no-order Render V40 shadow migration regressions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

import pytest

from app.t100_200_strategy import STRATEGY_ID
from app.t100_v40_render_stage2_scanner import (
    MAX_BATCH, V40_SHADOW_NEW_EVENTS_SQL, V40Stage2RenderScanner,
    V40RenderCutoverHalted, inspect_new_v40_stage2,
    require_v40_shadow_render_guards,
)

T=datetime(2026,11,11,9,0,tzinfo=timezone.utc)
PAPER={
    "T100_V40_RENDER_SCANNER_ENABLED":"true",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
}


def candidate():
    return {
        "id":42,"strategy_id":STRATEGY_ID,
        "symbol":"EXAMPLE_USDT","p2_at":T,
        "feature_at":T-timedelta(minutes=30),
        "created_at":T+timedelta(seconds=5),
        "entry_price":100.0,"physical_episode_seq":2,
        "stage_no":2,"stage1_return_24h":.20,
        "stage1_atr7_pct":5.8,
        "stage1_strict365":True,"stage1_eligible":True,
        "t100_tier":"HIGH_225","eligible_stage2":True,
        "trade_decision":"accepted",
    }


def candles():
    return [
        {
            "open_time":T-timedelta(minutes=30*i),
            "high":102.0,"close":100.0,"amount":100000.0,
        }
        for i in range(1,50)
    ]


class Context:
    async def __aenter__(self):return self
    async def __aexit__(self,*_):return False


class FakeConn:
    def __init__(self,rows=None,bars=None,role="mexc_scanner"):
        self.rows=[candidate()] if rows is None else rows
        self.bars=candles() if bars is None else bars
        self.role=role
        self.queries=[]
        self.readonly_txs=[]
    def transaction(self,*,isolation,readonly):
        self.readonly_txs.append((isolation,readonly))
        return Context()
    async def fetchval(self,sql):
        self.queries.append(sql)
        if "transaction_read_only" in sql:return "on"
        if "transaction_isolation" in sql:return "repeatable read"
        if "current_database" in sql:return "mexc_exhaustion"
        if "current_user" in sql:return self.role
        raise AssertionError("unknown SQL")
    async def fetch(self,sql,*args):
        self.queries.append(sql)
        if sql==V40_SHADOW_NEW_EVENTS_SQL:
            assert args==(T,MAX_BATCH+1)
            return deepcopy(self.rows)
        assert "FROM public.candles" in sql
        return deepcopy(self.bars)


class Pool:
    def __init__(self,conn):self.conn=conn
    def acquire(self):return ContextProxy(self.conn)


class ContextProxy:
    def __init__(self,conn):self.conn=conn
    async def __aenter__(self):return self.conn
    async def __aexit__(self,*_):return False


def audit(conn,*,evaluated=T,seen=T+timedelta(seconds=20)):
    return asyncio.run(inspect_new_v40_stage2(
        Pool(conn),eval_at=evaluated,observed_at=seen,
    ))


def test_new_render_entrypoint_requires_explicit_paper_kill():
    require_v40_shadow_render_guards(PAPER)
    for k,v in [
        ("T100_V40_RENDER_SCANNER_ENABLED","false"),
        ("T100_EXECUTION_MODE","real"),
        ("T100_LIVE_ORDER_API_ENABLED","true"),
        ("T100_LIVE_KILL_SWITCH","false"),
        ("EXECUTION_ENABLED","true"),
        ("TRADING_MODE","live"),
    ]:
        with pytest.raises(V40RenderCutoverHalted):
            require_v40_shadow_render_guards({**PAPER,k:v})


def test_pinned_scanner_owner_readonly_source_produces_no_trade_permission():
    conn=FakeConn()
    out=audit(conn)
    assert out.status=="V40_SHADOW_FRESH_STAGE2_CAUSAL_VOL30_AUDIT_NONAUTHORIZING"
    assert out.validated_stage2==1
    assert out.vol30_skip==0
    assert out.vol30_passed_research_only==1
    assert out.history_verified and out.source_readonly_transaction
    assert not out.entry_allowed and not out.live_authorization
    assert not out.signal_transport_to_executor_certified
    assert not out.account_risk_certified
    assert not out.native_stop_execution_certified
    assert out.mexc_posts==0
    assert conn.readonly_txs==[("repeatable_read",True)]
    assert all(q.startswith("SELECT") or q.lstrip().startswith("SELECT")
               for q in conn.queries)


def test_absent_current_signal_is_valid_empty_shadow():
    out=audit(FakeConn(rows=[]))
    assert out.validated_stage2==0
    assert out.vol30_passed_research_only==0
    assert out.history_verified
    assert not out.live_authorization


def test_invalid_causal_49_bar_batch_fails_without_partial_candidate():
    conn=FakeConn(bars=candles()[:-1])
    out=audit(conn)
    assert out.status=="V40_SHADOW_SIGNAL_OR_HISTORY_VALIDATION_HALTED"
    assert out.validated_stage2==0
    assert not out.history_verified
    assert not out.entry_allowed


def test_stale_evaluation_not_reused_for_live_candidate():
    conn=FakeConn()
    out=audit(conn,seen=T+timedelta(seconds=901))
    assert out.status=="V40_SHADOW_EVAL_NOT_CURRENT_NO_LIVE_CANDIDATE"
    assert not conn.queries
    assert out.mexc_posts==0


def test_role_guard_rejects_current_executor_role():
    conn=FakeConn(role="t100_live_runtime")
    out=audit(conn)
    assert out.status=="V40_SHADOW_DB_RESTRICTED_READ_CONTEXT_INVALID"
    assert not any("FROM public.t100_p2_signals" in q for q in conn.queries)
    assert not out.live_authorization


def test_more_than_32_candidate_events_rejected_not_silently_truncated():
    conn=FakeConn(rows=[{**candidate(),"id":n} for n in range(33)])
    out=audit(conn)
    assert out.status=="V40_SHADOW_SOURCE_BATCH_LIMIT_OR_SHAPE_INVALID"
    assert out.validated_stage2==0


def test_paper_trade_decision_is_not_a_v40_entry_gate():
    conn=FakeConn(rows=[{**candidate(),"trade_decision":"rejected_capacity"}])
    out=audit(conn)
    assert out.validated_stage2==1
    assert out.vol30_passed_research_only==1
    assert "trade_decision IS NULL" not in V40_SHADOW_NEW_EVENTS_SQL
    assert not out.entry_allowed


def test_legacy_performance_dashboard_intentionally_disabled():
    worker=object.__new__(V40Stage2RenderScanner)
    assert asyncio.run(worker._maybe_send_daily_dashboard(T)) is None
