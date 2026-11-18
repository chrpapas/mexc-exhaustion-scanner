"""Close-only durable fill/funding staging: positive venue proof, one SQL TX.

Offline FakeConn simulates serializable rollback and immutable full readback.
No real HTTP request, MEXC POST, database credentials or SQL mutations.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict,replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,LedgerSnapshot,
)
from app.t100_live_leader_fencing import LeaderLease
from app.t100_v40_at_entry_contract_specs import PostgresV40AtEntryContractSpecReader
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchRecovery,V40NativeCloseMismatchEvidence,
)
from app.t100_v40_portfolio_close_financial_reader import (
    V40NativeCloseFinancialReadPort,
)
from app import t100_v40_close_financial_artifact_staging as stage
from tests.test_t100_v40_portfolio_close_financial_reader import (
    environment,archived_entry,
)


class FakeConn:
    def __init__(self,previous):
        self.previous=previous
        self.data=dict(fill_headers={},fills={},funding_headers={},funding={})
        self.sql=[]
        self.role="t100_live_executor_runtime"
        self.holder="unit-test-fenced"
        self.token=7
        self.readonly="off"
        self.tx=[]
        self.modified=False
        self.break_fill=False
    def acquire(self):return self
    def transaction(self,*,isolation):
        assert isolation=="serializable"
        class Context:
            async def __aenter__(x):
                self.tx.append(deepcopy(self.data))
                return self
            async def __aexit__(x,kind,err,tb):
                if kind:
                    self.data=self.tx[-1]
                self.tx.pop()
        return Context()
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def execute(self,sql,*args):
        assert "pg_advisory_xact_lock" in sql
        self.sql.append("ACCOUNT_LOCK")
    async def fetchval(self,sql,*args):
        if sql=="SELECT current_database()":return "mexc_exhaustion"
        if sql=="SELECT current_user":return self.role
        if sql.startswith("SELECT current_setting("):return self.readonly
        if sql.startswith("SELECT COUNT(*)::int"):
            return 0
        if "INSERT INTO public.t100_live_order_fill_sets" in sql:
            keys=("order_id","position_id","symbol","side","order_state",
                  "deal_volume","average_price","fee_currency","fill_count",
                  "fill_digest_sha256","strategy_fully_filled_order_complete")
            self.data["fill_headers"].setdefault(args[0],dict(zip(keys,args)))
            self.sql.append("FILL_HEADER")
            return args[0]
        if "INSERT INTO public.t100_live_order_fills" in sql:
            keys=("trade_id","order_id","position_id","side","contracts","price",
                  "fee_usdt","timestamp_ms","category")
            data=dict(zip(keys,args))
            if self.break_fill:data["fee_usdt"]=D("999")
            self.data["fills"].setdefault(args[0],data)
            self.sql.append("FILL_ROW")
            return args[0]
        if "INSERT INTO public.t100_live_position_funding_sets" in sql:
            keys=("position_id","total_count","funding_digest_sha256",
                  "economics_verified","double_collection_finality_verified")
            self.data["funding_headers"].setdefault(args[0],dict(zip(keys,args)))
            self.sql.append("FUNDING_HEADER")
            return args[0]
        if "INSERT INTO public.t100_live_funding_events" in sql:
            keys=("event_id","position_id","symbol","position_type",
                  "position_value","funding_cashflow_usdt","rate","settle_time_ms")
            self.data["funding"].setdefault(args[0],dict(zip(keys,args)))
            self.sql.append("FUNDING_ROW")
            return args[0]
        raise AssertionError("unexpected SQL fetchval "+sql[:90])
    async def fetchrow(self,sql,*args):
        if "t100_live_executor_leases" in sql:
            return {"holder_id":self.holder,"fencing_token":self.token,
                    "unexpired":True}
        if "FROM public.t100_live_position_checkpoints" in sql:
            return asdict(self.previous)
        if "FROM public.t100_live_order_fill_sets" in sql:
            return self.data["fill_headers"].get(args[0])
        if "FROM public.t100_live_position_funding_sets" in sql:
            return self.data["funding_headers"].get(args[0])
        raise AssertionError("unexpected SQL fetchrow "+sql[:90])
    async def fetch(self,sql,*args):
        if "FROM public.t100_live_order_fills" in sql:
            return sorted(
                (x for x in self.data["fills"].values() if x["order_id"]==args[0]),
                key=lambda x:(x["timestamp_ms"],x["trade_id"]),
            )
        if "FROM public.t100_live_funding_events" in sql:
            return sorted(
                (x for x in self.data["funding"].values() if x["position_id"]==args[0]),
                key=lambda x:(x["settle_time_ms"],x["event_id"]),
            )
        raise AssertionError("unexpected SQL fetch "+sql[:90])


def configure(monkeypatch):
    previous,witness,finance,reads,funding_calls=environment(monkeypatch)
    pool=FakeConn(previous)
    ledger=object.__new__(PostgresFourSlotLedgerReader)
    ledger.pool=pool
    checkpoint=asdict(previous)
    async def snapshot():
        return LedgerSnapshot([deepcopy(checkpoint)],[],[],[],True)
    ledger.read_snapshot=snapshot
    archive=object.__new__(PostgresV40AtEntryContractSpecReader)
    archive.pool=pool
    async def archived(previous):
        return archived_entry(previous)
    archive.read=archived
    finance.entry_contracts=archive
    venue=object.__new__(FourSlotMexcReadVenue)
    venue.probe=finance.probe
    native=object.__new__(V40NativeCloseMismatchRecovery)
    native.ledger=ledger
    native.venue=venue
    native.probe=finance.probe
    native.scope="a"*64
    async def observed(snapshot):
        assert snapshot.status=="UNOWNED_OR_MISSING_CHECKPOINTS"
        return V40NativeCloseMismatchEvidence(
            status="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING",
            previous=previous,survivors=(),witness=witness,
        )
    native.observe=observed
    port=stage.V40CloseOnlyFinancialArtifactStaging(
        native=native,finance=finance,ledger=ledger,
    )
    lease=LeaderLease("t100-live-executor",pool.holder,pool.token,45)
    snapshot=FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
    return SimpleNamespace(
        port=port,ledger=ledger,finance=finance,native=native,
        db=pool,lease=lease,snapshot=snapshot,previous=previous,
        witness=witness,reads=reads,
    )


@pytest.mark.asyncio
async def test_full_real_order_deals_and_paginated_funding_staged_atomically(
    monkeypatch,
):
    x=configure(monkeypatch)
    out=await x.port.stage_once(snapshot=x.snapshot,lease=x.lease)
    assert out.status=="V40_CLOSE_FILLS_AND_FUNDING_DURABLY_STAGED_FINAL_ACCOUNTING_NOT_CERTIFIED"
    assert out.position_id==x.previous.position_id
    assert out.immutable_entry_fills_verified
    assert out.immutable_close_fills_verified
    assert out.immutable_funding_verified
    assert not out.final_accounting_verified
    assert not out.historical_multiplier_continuity_certified
    assert not out.slot_released and not out.next_entry_allowed
    assert not out.live_authorization and not out.mexc_posts
    assert out.database_commit_may_have_occurred
    assert len(x.db.data["fill_headers"])==2
    assert len(x.db.data["fills"])==2
    assert len(x.db.data["funding_headers"])==1
    assert len(x.db.data["funding"])==1
    assert not x.db.tx
    assert x.db.sql.count("ACCOUNT_LOCK")==1


@pytest.mark.asyncio
async def test_existing_immutable_rows_fully_identical_are_safe_to_inspect(
    monkeypatch,
):
    x=configure(monkeypatch)
    await x.port.stage_once(snapshot=x.snapshot,lease=x.lease)
    before=deepcopy(x.db.data)
    second=await x.port.stage_once(snapshot=x.snapshot,lease=x.lease)
    assert second.immutable_funding_verified
    assert x.db.data==before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption",[
    "different_lease","readonly","wrong_role","wrong_entry_origin",
    "different_current_funding","different_existing_fill",
    "external_fill_corrupt","future_multiplier_certification",
])
async def test_fail_closed_with_full_transaction_rollback(monkeypatch,corruption):
    x=configure(monkeypatch)
    if corruption in ("different_existing_fill","different_current_funding"):
        await x.port.stage_once(snapshot=x.snapshot,lease=x.lease)
    original=deepcopy(x.db.data)
    if corruption=="different_lease":
        x.db.token=99
    elif corruption=="readonly":
        x.db.readonly="on"
    elif corruption=="wrong_role":
        x.db.role="mexc_scanner"
    elif corruption=="wrong_entry_origin":
        x.db.previous=replace(x.previous,exchange_order_id="999")
    elif corruption=="different_current_funding":
        x.db.data["funding_headers"][x.previous.position_id]["funding_digest_sha256"]="f"*64
    elif corruption=="different_existing_fill":
        x.db.data["fill_headers"]["100"]["fee_currency"]="MX"
    elif corruption=="external_fill_corrupt":
        x.db.break_fill=True
    elif corruption=="future_multiplier_certification":
        async def fake_financial(*,previous,witness):
            original=await V40NativeCloseFinancialReadPort.observe(x.finance,previous=previous,witness=witness)
            return replace(original,historical_contract_multiplier_certified=True)
        x.finance.observe=fake_financial
    expected=deepcopy(x.db.data)
    with pytest.raises(stage.V40CloseArtifactHalted):
        await x.port.stage_once(snapshot=x.snapshot,lease=x.lease)
    assert x.db.data==expected
    assert x.db.tx==[]


def test_same_account_source_and_archive_are_mandatory(monkeypatch):
    x=configure(monkeypatch)
    other=object.__new__(PostgresV40AtEntryContractSpecReader)
    other.pool=object()
    x.finance.entry_contracts=other
    with pytest.raises(stage.V40CloseArtifactHalted,match="SAME_ACCOUNT"):
        stage.V40CloseOnlyFinancialArtifactStaging(
            native=x.native,finance=x.finance,ledger=x.ledger,
        )


@pytest.mark.asyncio
async def test_no_lease_blocks_before_private_get_or_sql(monkeypatch):
    x=configure(monkeypatch)
    with pytest.raises(stage.V40CloseArtifactHalted,match="LEADER_REQUIRED"):
        await x.port.stage_once(snapshot=x.snapshot,lease=object())
    assert not x.reads
    assert x.db.sql==[]
