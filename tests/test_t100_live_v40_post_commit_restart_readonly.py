"""Offline tests for opt-in V40 SQL/GET-only native-market restart path."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.t100_live_v40_post_commit_restart_readonly as m
from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot
from tests.test_t100_live_v40_post_commit_restart_audit import expected

PAPER={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}


class Ledger:
    def __init__(self,checkpoints=None,fail=False):
        self.checkpoints=checkpoints if checkpoints is not None else []
        self.fail=fail
        self.reads=0
    async def read_snapshot(self):
        self.reads+=1
        if self.fail:
            raise RuntimeError("PRIVATE_DB_DSN")
        return LedgerSnapshot(
            checkpoints=self.checkpoints,
            revisions=[],intents=[],stop_amendments=[],
            readonly_repeatable_read=True,
        )


class ForbiddenClient:
    def __init__(self):self.calls=[]
    async def _private_get(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        raise AssertionError("NO_EXCHANGE_GET_BEFORE_CHECKPOINT")


def run(*,ledger=None,client=None,env=None,position_id="10"):
    return asyncio.run(m.run_readonly_v40_post_commit_restart_audit(
        ledger=ledger or Ledger(),
        client=client or ForbiddenClient(),
        env=PAPER if env is None else env,
        position_id=position_id,
    ))


def test_flat_account_missing_durable_checkpoint_refuses_before_exchange_get():
    ledger=Ledger([])
    client=ForbiddenClient()
    out=run(ledger=ledger,client=client)
    assert out["status"]=="POST_COMMIT_EXPECTED_OWNED_CHECKPOINT_ABSENT_HALT"
    assert ledger.reads==1
    assert client.calls==[]
    assert not out["actual_process_restart_verified"]
    assert not out["live_authorization"]
    assert out["mexc_posts"]==0


def test_not_yet_amended_checkpoint_refuses_without_exchange_get():
    from dataclasses import replace
    prior=replace(expected(),checkpoint_version=1,armed=False,
                  best_profit_pct=0)
    ledger=Ledger([asdict(prior)])
    client=ForbiddenClient()
    out=run(ledger=ledger,client=client)
    assert out["status"]=="POST_COMMIT_EXPECTED_TRAILING_REVISION_NOT_READY_HALT"
    assert client.calls==[]


def test_read_only_restricted_db_failure_is_redacted():
    out=run(ledger=Ledger(fail=True))
    assert out["status"]=="POST_COMMIT_DURABLE_LEDGER_PRECHECK_FAILED_HALT"
    assert "PRIVATE_DB_DSN" not in str(out)
    assert out["database_writes"]==0


@pytest.mark.parametrize("case",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {},
])
def test_unsafe_runtime_refuses_without_reading_ledger_or_exchange(case):
    ledger=Ledger([asdict(expected())])
    client=ForbiddenClient()
    out=run(ledger=ledger,client=client,env={**PAPER,**case}
            if case else {})
    assert out["status"]=="POST_COMMIT_PAPER_KILL_REQUIRED_HALT"
    assert ledger.reads==0
    assert client.calls==[]


@pytest.mark.parametrize("identifier",[
    None,0,False,"", "0","-12","0010","1/../2","１０",
])
def test_invalid_operator_position_id_refuses_before_ledger_and_exchange(identifier):
    ledger=Ledger([asdict(expected())])
    client=ForbiddenClient()
    out=run(ledger=ledger,client=client,position_id=identifier)
    assert out["status"]=="POST_COMMIT_EXPECTED_POSITION_ID_INVALID_HALT"
    assert ledger.reads==0
    assert client.calls==[]


def test_good_offline_observation_does_not_become_live_authorization(monkeypatch):
    ledger=Ledger([asdict(expected())])
    client=ForbiddenClient()

    class Audit:
        def __init__(self,snapshot,*,probe):
            assert snapshot.ledger is ledger
            assert probe is not None
        async def observe(self,*,expected):
            return SimpleNamespace(
                status="POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING",
                expected_checkpoint_recovered=True,
                two_independent_full_account_snapshots_matched=True,
                native_market_stop_type_verified_after_restart=True,
                recovered_slots=1,
                live_authorization=False,entry_allowed=False,
                exchange_posts=0,db_writes=0,
            )

    monkeypatch.setattr(m,"V40PostCommitRestartAuditor",Audit)
    out=run(ledger=ledger,client=client)
    assert out["status"].endswith("_NONAUTHORIZING")
    assert out["two_stable_nonflat_snapshot_rounds_verified"]
    assert out["native_market_sl_all_owned_slots_get_verified"]
    assert out["recovered_owned_slots"]==1
    assert out["actual_process_restart_verified"] is False
    assert out["continuous_protection_during_amendment_certified"] is False
    assert out["live_authorization"] is False
    assert out["entry_allowed"] is False
    assert out["database_writes"]==0
    assert out["mexc_posts"]==0


def test_pretend_success_that_enables_orders_is_rejected(monkeypatch):
    ledger=Ledger([asdict(expected())])
    class Audit:
        def __init__(self,*args,**kw):pass
        async def observe(self,**kwargs):
            return SimpleNamespace(
                status="POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING",
                expected_checkpoint_recovered=True,
                two_independent_full_account_snapshots_matched=True,
                native_market_stop_type_verified_after_restart=True,
                recovered_slots=1,
                live_authorization=True,entry_allowed=True,
                exchange_posts=1,db_writes=1,
            )
    monkeypatch.setattr(m,"V40PostCommitRestartAuditor",Audit)
    out=run(ledger=ledger)
    assert out["status"]=="POST_COMMIT_RESTART_OUTPUT_INVARIANT_FAILED_FREEZE"
    assert out["mexc_posts"]==0
    assert not out["live_authorization"]


def test_unknown_upstream_status_never_leaks_private_values(monkeypatch):
    ledger=Ledger([asdict(expected())])
    class Audit:
        def __init__(self,*args,**kw):pass
        async def observe(self,**kwargs):
            return SimpleNamespace(status="PRIVATE_MEXC_SECRET_RETURNED")
    monkeypatch.setattr(m,"V40PostCommitRestartAuditor",Audit)
    out=run(ledger=ledger)
    assert out["status"]=="POST_COMMIT_RESTART_UNRECOGNIZED_PROVENANCE_FREEZE"
    assert "PRIVATE_MEXC_SECRET" not in str(out)
