"""Offline no-order regression for claim-bound V40 GET -> SQL checkpoint bridge."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from app import t100_live_v40_post_dispatch_reconcile as bridge
from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_four_slot_protected_get_evidence import ProtectedGetPreparation
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_v40_protected_commit import ProtectedCommitResult
from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch, PreparedProtectedEntry,
)


def fixtures(monkeypatch):
    intent = EntryIntent(
        42, "SIREN_USDT", D("100"), D("100"), D("1"), D("175"),
        "t100v1e" + "a"*20,
    )
    stage = DurableAttemptStage(42, intent.external_oid)
    batch = PreparedProtectedBatch(
        entries=(PreparedProtectedEntry(
            stage, intent, "1001", "2001", None, None,
        ),),
    )
    prepared = ProtectedGetPreparation(
        "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        prepared_batch=batch, orders_observed=1,
        deal_histories_observed=1, two_bookends_equal=True,
        dispatch_claims_verified=True,
    )
    get = AsyncMock(return_value=prepared)
    monkeypatch.setattr(
        bridge, "observe_claimed_staged_protection_get_only", get,
    )
    class Writer:
        def __init__(self):
            self.calls=0
            self.fail=False
            self.answer=ProtectedCommitResult(
                "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING",
                entries_committed=1, db_atomic_commit_observed=True,
            )
        async def persist_claimed(self, **kwargs):
            self.calls += 1
            if self.fail:
                raise ConnectionError("DB commit may have succeeded")
            return self.answer
    writer=Writer()
    args=dict(
        stages=(stage,), intents=(intent,),
        pre_attempt_witness=FourSlotSnapshotResult(
            status="FLAT_OBSERVED_NONAUTHORIZING",
        ),
        claim_reader=object(), venue=object(), completeness=object(),
        commit_writer=writer,
        lease=LeaderLease("t100-live-executor", "worker", 7, 30),
        observed_at=datetime.now(timezone.utc),
    )
    return args, get, writer, prepared


def invoke(args):
    return asyncio.run(bridge.reconcile_and_commit_protected_v40(**args))


def test_exact_claimed_get_commits_once_without_order_transport(monkeypatch):
    args, get, writer, _ = fixtures(monkeypatch)
    out=invoke(args)
    assert out.status == "V40_PROTECTED_FILL_AND_CHECKPOINT_PERSISTED_RESTART_REQUIRED"
    assert out.records_committed == 1
    assert out.post_commit_restart_verified is False
    assert out.entry_allowed is False
    assert out.live_authorization is False
    assert out.resubmission_allowed is False
    assert out.mexc_orders_sent is False
    assert get.await_count == 1
    assert writer.calls == 1


@pytest.mark.parametrize("changed", [
    {"dispatch_claims_verified": False},
    {"two_bookends_equal": False},
    {"read_only": False},
    {"live_authorization": True},
    {"mexc_orders_sent": True},
    {"status": "PROTECTED_GET_ORDER_UNKNOWN_NO_RETRY"},
])
def test_non_authorizing_claim_get_required(monkeypatch, changed):
    args, get, writer, prepared = fixtures(monkeypatch)
    get.return_value=replace(prepared, **changed)
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_PROTECTION_GET_NOT_CERTIFIED_NO_RETRY"
    assert writer.calls == 0
    assert out.resubmission_allowed is False


def test_unknown_db_response_blocks_recommit(monkeypatch):
    args, _, writer, _ = fixtures(monkeypatch)
    writer.fail=True
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_COMMIT_UNKNOWN_MANUAL_RECONCILE_ONLY"
    assert out.claim_bound_get_verified is True
    assert out.records_committed == 0
    assert out.resubmission_allowed is False
    assert writer.calls == 1


def test_inconsistent_commit_report_is_not_accepted(monkeypatch):
    args, _, writer, _ = fixtures(monkeypatch)
    writer.answer=replace(writer.answer, entries_committed=2)
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_COMMIT_RESPONSE_UNVERIFIED_RECHECK_SQL"
    assert out.live_authorization is False


def test_claimed_batch_must_keep_exact_stage_and_intent(monkeypatch):
    args, get, writer, prepared = fixtures(monkeypatch)
    item=prepared.prepared_batch.entries[0]
    tampered=replace(item, stage=replace(item.stage, signal_id=43))
    get.return_value=replace(prepared, prepared_batch=replace(
        prepared.prepared_batch, entries=(tampered,),
    ))
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_PREPARED_ENTRY_DRIFT_NO_RETRY"
    assert writer.calls == 0


def test_bad_fence_blocks_before_get_or_db(monkeypatch):
    args, get, writer, _ = fixtures(monkeypatch)
    args["lease"]=replace(args["lease"], fencing_token=0)
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_INPUT_INVALID_NO_RETRY"
    assert get.await_count == 0
    assert writer.calls == 0


def test_unavailable_exchange_get_never_commits(monkeypatch):
    args, get, writer, _ = fixtures(monkeypatch)
    get.side_effect=ConnectionError("private venue error")
    out=invoke(args)
    assert out.status == "V40_POST_DISPATCH_GET_FAILED_NO_RETRY"
    assert writer.calls == 0
    assert "private venue error" not in str(out)
