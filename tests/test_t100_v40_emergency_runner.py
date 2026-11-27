"""Supervised V40 emergency runner: no entry, one close, restart GET-only."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.t100_v40_emergency_runner as m
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_emergency_observer import V40EmergencyObservation
from app.t100_v40_emergency_close import V40EmergencyCloseAck
from tests.test_t100_v40_emergency_close import target,frame
from tests.test_t100_v40_emergency_postflight import order,fills,flat


def env():
    return {
        m.FLAG:"true",
        "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
        "T100_V40_SINGLE_POSITION_PILOT":"true",
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "TRADING_MODE":"live","EXECUTION_ENABLED":"true",
        "T100_V40_EXECUTOR_DISABLED_WORKER":"false",
    }


def probe():
    raw=SimpleNamespace(
        _key="FAKE_TEST_PILOT_KEY",
        _secret="FAKE_TEST_PILOT_SECRET_LONG",
    )
    return ReadOnlyMexcProbe(raw)


def venue():
    posts=[]
    async def post(*args,**kwargs):
        posts.append((args,kwargs))
        return "7901"
    raw=SimpleNamespace(
        api_key="FAKE_TEST_PILOT_KEY",
        api_secret="FAKE_TEST_PILOT_SECRET_LONG",
        _private_post=post,
    )
    return raw,posts


class Claim:
    def __init__(self,existing=None):
        self.existing=existing
        self.reserve_count=0
    async def read_prior_claim(self):
        return self.existing
    async def reserve_one(self,t):
        self.reserve_count+=1
        if self.existing is not None:
            return False
        self.existing=t
        return True


def _run(monkeypatch,*,prior=None,
         observation="missing",postflight_status="pending"):
    db=object()
    client,posts=venue()
    witness=probe()
    claimant=Claim(existing=prior)
    monkeypatch.setattr(m,"V40PostgresEmergencyCloseClaims",lambda pool:claimant)
    events=[]
    async def observed(**kwargs):
        events.append("READ_DOUBLE_SIGNED")
        if observation=="missing":
            return V40EmergencyObservation(
                "V40_EMERGENCY_OWNED_SHORT_MISSING_NATIVE_SL_TWO_GETS",
                target=target(),
            )
        return V40EmergencyObservation("V40_EMERGENCY_CLOSE_NO_SINGLE_DURABLE_OWNER")
    monkeypatch.setattr(m,"observe_emergency_target",observed)
    async def reconciliation(**kwargs):
        events.append("GET_ONLY_RECONCILE")
        assert claimant.existing is not None
        return m.V40EmergencyRunnerStatus(
            "V40_EMERGENCY_CLOSE_GET_OR_DB_RECONCILIATION_UNVERIFIED",
            claim_committed=True,exchange_close_post_may_have_occurred=True,
        )
    monkeypatch.setattr(m,"_reconcile_only",reconciliation)
    result=asyncio.run(m.run_v40_emergency_close_once(
        env(),pool=db,probe=witness,public=object(),venue=client,
    ))
    return result,posts,claimant,events


def test_disabled_or_wrong_account_never_instantiates_any_claim(monkeypatch):
    bad=env()
    bad[m.FLAG]="false"
    client,posts=venue()
    result=asyncio.run(m.run_v40_emergency_close_once(
        bad,pool=object(),probe=probe(),public=object(),venue=client,
    ))
    assert result.status=="V40_EMERGENCY_EXIT_ACCOUNT_OR_LIVE_CONTROLS_REQUIRED"
    assert posts==[]
    bad=env()
    bad["T100_V40_ACCOUNT_PROFILE"]="BOXWAVE"
    assert not m.emergency_environment(bad)


def test_existing_committed_claim_only_reconciles_get_no_second_close(monkeypatch):
    result,posts,claim,events=_run(monkeypatch,prior=target())
    assert result.claim_committed
    assert result.exchange_close_post_may_have_occurred
    assert posts==[]
    assert claim.reserve_count==0
    assert events==["GET_ONLY_RECONCILE"]


def test_no_verified_missing_native_stop_does_not_close(monkeypatch):
    result,posts,claim,events=_run(monkeypatch,observation="healthy")
    assert result.status=="V40_EMERGENCY_EXIT_NO_CERTIFIED_MISSING_STOP_NO_POST"
    assert posts==[] and claim.reserve_count==0
    assert events==["READ_DOUBLE_SIGNED"]


def test_true_original_missing_stop_only_one_close_after_claim(monkeypatch):
    result,posts,claim,events=_run(monkeypatch)
    assert len(posts)==1
    assert claim.reserve_count==1
    assert events==["READ_DOUBLE_SIGNED","READ_DOUBLE_SIGNED","GET_ONLY_RECONCILE"]
    body=posts[0][0][1]
    assert body["side"]==2 and body["openType"]==2
    assert body["positionId"]==9801
    assert body["externalOid"]==target().close_external_oid
    assert "reduceOnly" not in body
    assert result.claim_committed
    assert result.exchange_close_post_may_have_occurred


def test_venue_api_identity_mismatch_stops_before_claim(monkeypatch):
    client,posts=venue()
    client.api_key="DIFFERENT_ACCOUNT"
    result=asyncio.run(m.run_v40_emergency_close_once(
        env(),pool=object(),probe=probe(),public=object(),venue=client,
    ))
    assert result.status=="V40_EMERGENCY_EXIT_SIGNED_ACCOUNT_IDENTITY_INVALID"
    assert posts==[]


def test_reconcile_missing_exact_oid_never_post_or_finish(monkeypatch):
    witness=probe()
    async def missing(symbol,external_oid):
        return None
    witness.raw_order_by_external_oid=missing
    async def ban(*args,**kwargs):
        raise AssertionError("NO_OTHER_GET_SHOULD_RUN")
    witness.raw_order_deal_details=ban
    status=asyncio.run(m._reconcile_only(
        target=target(),probe=witness,pool=object(),existing_claim=True,
    ))
    assert status.status=="V40_EMERGENCY_CLOSE_UNKNOWN_POST_NO_ORDER_RETRY"
    assert status.exchange_close_post_may_have_occurred
    assert not status.durable_checkpoint_and_slot_closed


def test_only_completed_close_order_and_double_flat_signatures_can_finish(monkeypatch):
    witness=probe()
    async def exact_order(symbol,external_oid):
        assert external_oid==target().close_external_oid
        return order()
    async def exact_deals(order_id):
        return fills()
    witness.raw_order_by_external_oid=exact_order
    witness.raw_order_deal_details=exact_deals
    async def bookends(probe,symbol):
        return flat()
    monkeypatch.setattr(m,"_signed_frame",bookends)
    rows=[]
    class Completion:
        def __init__(self,pool):pass
        async def finalize_verified(self,*,target,postflight):
            rows.append(postflight.status)
            return SimpleNamespace(
                status="V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED"
            )
    monkeypatch.setattr(m,"PostgresV40EmergencyCompletion",Completion)
    async def pause(_):pass
    status=asyncio.run(m._reconcile_only(
        target=target(),probe=witness,pool=object(),
        existing_claim=True,delay=pause,
    ))
    assert status.signed_close_and_flat_verified
    assert status.durable_checkpoint_and_slot_closed
    assert rows==["V40_EMERGENCY_CLOSE_ORDER_AND_FLAT_ACCOUNT_DOUBLE_GET_VERIFIED"]
    assert not status.new_entries_allowed


def test_close_order_with_remaining_position_does_not_close_checkpoint(monkeypatch):
    witness=probe()
    async def exact_order(symbol,external_oid):return order()
    async def exact_deals(order_id):return fills()
    witness.raw_order_by_external_oid=exact_order
    witness.raw_order_deal_details=exact_deals
    count=0
    async def bookends(probe,symbol):
        nonlocal count
        count+=1
        return flat() if count==1 else frame()
    monkeypatch.setattr(m,"_signed_frame",bookends)
    class NoFinalize:
        def __init__(self,pool):raise AssertionError("NEVER_CLOSE_DB")
    monkeypatch.setattr(m,"PostgresV40EmergencyCompletion",NoFinalize)
    async def pause(_):pass
    status=asyncio.run(m._reconcile_only(
        target=target(),probe=witness,pool=object(),
        existing_claim=True,delay=pause,
    ))
    assert status.status=="V40_EMERGENCY_CLOSE_POST_ACK_AWAIT_SIGNED_FLAT_RECONCILIATION"
    assert not status.durable_checkpoint_and_slot_closed
