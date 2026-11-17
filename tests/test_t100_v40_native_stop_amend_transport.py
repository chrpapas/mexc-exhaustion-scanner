"""Offline contract/identity tests for MEXC native SL amendment adapter.

No credentials, network or trading. Patches an unreleased guard ONLY inside
offline tests to prove the one-attempt payload and durable CAS boundary.
"""
from __future__ import annotations

from dataclasses import replace, fields
from decimal import Decimal as D
from unittest.mock import patch
import pytest

from app.t100_live_guards import T100ExecutionControls, LIVE_CONFIRMATION
from app.t100_live_authorization import (
    V40FirstCanaryReadinessEvidence, issue_v40_supervised_canary_permit,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_snapshot_coordinator import _venue_digest
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_mutation_executor import StopMutationExecutor, StopMutationExecutionHalted
from app.t100_live_supervisor import StopProposal
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_v40_unreleased_nonflat_guardian_evidence import V40NonflatGuardianEvidence
from app import t100_v40_native_stop_amend_transport as m


def checkpoint():
    return PositionCheckpoint(
        symbol="SIREN_USDT",external_oid="source",exchange_order_id="91",
        position_id="52",contracts=D("1"),entry_price=D("100"),
        stop_order_id="92",stop_price=D("175"),best_profit_pct=D("14"),
        armed=True,lifecycle_state="OPEN",checkpoint_version=2,
    )


def row_position():
    return {
        "positionId":52,"symbol":"SIREN_USDT","positionType":2,
        "openType":2,"leverage":1,"state":1,"holdVol":1,
    }


def row_stop():
    return {
        "id":92,"positionId":52,"orderId":91,"symbol":"SIREN_USDT",
        "positionType":2,"state":1,"isFinished":0,"triggerSide":0,
        "errorCode":0,"stopLossType":0,"stopLossReverse":2,"lossTrend":1,
        "stopLossPrice":"175","volType":1,
        "profitLossVolType":"SEPARATE","stopLossVol":1,
    }


def preflight():
    return TrailingProposalReview(
        "TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING",
        proposal=StopProposal(
            symbol="SIREN_USDT",position_id="52",stop_order_id="92",
            old_stop=D("175"),proposed_stop=D("99"),
            best_profit_pct=D("14"),armed=True,
        ),
        account_bookends_equal=True,
        all_owned_full_volume_market_stops_observed=True,
        frozen_trailing_policy_matched=True,
        current_stop_above_last_price=True,
        market_low_provenance_verified=True,
        signed_get_provenance_independently_verified=True,
    )


def healthy_guardian():
    return V40NonflatGuardianEvidence(
        "V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING",
        guardian_protected_observation_fresh=True,
        no_current_scope_critical_or_pending_incidents=True,
    )


def canary_permit():
    values={
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"12.50",
        "T100_LIVE_LEVERAGE":"1",
    }
    control=T100ExecutionControls.from_env(values)
    attrs={x.name:True for x in fields(V40FirstCanaryReadinessEvidence)}
    evidence=V40FirstCanaryReadinessEvidence(**attrs)
    return issue_v40_supervised_canary_permit(
        control,evidence,signal_id=497,symbol="SIREN_USDT",
    )


class Client:
    def __init__(self):
        self.positions=[row_position()]
        self.stops=[row_stop()]
        self.counts={
            "sumCount":1,"stopOrderCount":1,"limitOrderCount":0,
            "planOrderCount":0,"trackOrderCount":0,
        }
        self.gets=0
        self.posts=[]

    async def _private_get(self,path,params=None):
        self.gets+=1
        if path=="/api/v1/private/position/open_positions":
            return list(self.positions)
        if path=="/api/v1/private/stoporder/open_orders":
            return list(self.stops)
        if path=="/api/v1/private/order/open_order_total_count":
            return dict(self.counts)
        if path=="/api/v1/private/position/position_mode":
            return 1
        raise AssertionError("EXTRA_GET_ENDPOINT")


async def cert(probe):
    client=probe._client
    return {
        "account_wide_completeness_verified":True,
        "critical_inventory_equal_across_witness_window":True,
        "critical_inventory_sha256":_venue_digest(client.positions,client.stops),
        "exchange_atomicity_verified":False,
        "entry_allowed":False,
        "live_authorization":False,
        "before_positions":len(client.positions),
        "after_positions":len(client.positions),
        "before_current_tpsl":len(client.stops),
        "after_current_tpsl":len(client.stops),
    }


def transport(client, **kwargs):
    return m.V40DocumentedNativeStopAmendTransport(
        client=client,checkpoints=kwargs.get("checkpoints",(checkpoint(),)),
        target_position_id=kwargs.get("target_position_id","52"),
        target_signal_id=kwargs.get("target_signal_id",497),
        preflight=kwargs.get("preflight",preflight()),
        guardian=kwargs.get("guardian",healthy_guardian()),
    )


async def execute(t, permit=None):
    return await t.submit_native_short_stop_tightening(
        stop_order_id=92,previous_stop_price="175",
        new_stop_price="99",permit=permit or canary_permit(),
    )


@pytest.mark.asyncio
async def test_unreleased_guard_rejects_before_any_get_or_post():
    client=Client()
    client._private_post=post_for(client)
    t=transport(client)
    with pytest.raises(
        m.V40StopAmendmentContractHalted,match="NOT_CERTIFIED",
    ):
        await execute(t)
    assert client.gets==0
    assert client.posts==[]


def post_for(client):
    async def post(path,body):
        client.posts.append((path,body))
        return None
    return post


@pytest.mark.asyncio
async def test_documented_same_native_id_tighten_payload_and_two_bookends(monkeypatch):
    client=Client()
    client._private_post=post_for(client)
    monkeypatch.setattr(m,"run_current_inventory_completeness_probe",cert)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        result=await execute(transport(client))
    assert result is None  # REST acknowledgement is NOT verified finality
    assert client.gets>=8
    assert client.posts==[(
        "/api/v1/private/stoporder/change_plan_price",
        {
            "stopPlanOrderId":92,"stopLossPrice":"99",
            "lossTrend":1,"stopLossReverse":2,
        },
    )]
    assert "takeProfitPrice" not in client.posts[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("modify",[
    lambda c:c.stops[0].__setitem__("stopLossType",1),
    lambda c:c.stops[0].pop("stopLossType"),
    lambda c:c.stops[0].__setitem__("stopLossReverse",1),
    lambda c:c.stops[0].__setitem__("stopLossVol",D("0.5")),
    lambda c:c.stops[0].__setitem__("stopLossPrice","174"),
    lambda c:c.stops[0].__setitem__("positionId",53),
    lambda c:c.positions[0].__setitem__("positionType",1),
    lambda c:c.positions[0].__setitem__("openType",1),
    lambda c:c.counts.__setitem__("limitOrderCount",1),
    lambda c:c.positions.append(row_position()),
])
async def test_missing_native_coverage_or_inventory_ambiguity_never_posts(monkeypatch,modify):
    client=Client()
    client._private_post=post_for(client)
    modify(client)
    monkeypatch.setattr(m,"run_current_inventory_completeness_probe",cert)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(m.V40StopAmendmentContractHalted):
            await execute(transport(client))
    assert client.posts==[]


@pytest.mark.asyncio
async def test_guardian_latched_no_get_no_post(monkeypatch):
    client=Client()
    client._private_post=post_for(client)
    g=replace(healthy_guardian(),no_current_scope_critical_or_pending_incidents=False)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(
            m.V40StopAmendmentContractHalted,match="CURRENT_PROVENANCE_UNVERIFIED",
        ):
            await execute(transport(client,guardian=g))
    assert client.gets==0 and client.posts==[]


@pytest.mark.asyncio
async def test_cannot_promote_one_position_canary_permit_to_four_slots():
    client=Client()
    client._private_post=post_for(client)
    other=replace(
        checkpoint(),symbol="OTHER_USDT",position_id="53",
        stop_order_id="93",exchange_order_id="94",
    )
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(
            m.V40StopAmendmentContractHalted,match="CANARY_SCOPE_OR_IDENTITY_INVALID",
        ):
            await execute(transport(client,checkpoints=(checkpoint(),other)))
    assert client.gets==0 and client.posts==[]


@pytest.mark.asyncio
async def test_wrong_signal_permit_cannot_amend_native_stop():
    client=Client()
    client._private_post=post_for(client)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(
            m.V40StopAmendmentContractHalted,match="CANARY_SCOPE_OR_IDENTITY_INVALID",
        ):
            await execute(transport(client,target_signal_id=499))
    assert client.gets==0 and client.posts==[]


class MemoryJournal:
    def __init__(self):
        self.rows={}
    async def prepare(self,amendment):
        if amendment.external_id in self.rows:
            return False
        self.rows[amendment.external_id]="PREPARED"
        return True
    async def state(self,external_id):
        return self.rows[external_id]
    async def transition(self,external_id,expected,target):
        if self.rows[external_id]!=expected:
            return False
        self.rows[external_id]=target
        return True


@pytest.mark.asyncio
async def test_durable_submitting_precedes_single_post_and_no_repeat(monkeypatch):
    client=Client()
    client._private_post=post_for(client)
    journal=MemoryJournal()
    lifecycle=StopAmendmentLifecycleCoordinator(journal)
    amendment,status=await lifecycle.prepare(preflight().proposal)
    assert status=="PREPARED"
    executor=StopMutationExecutor(lifecycle=lifecycle,transport=transport(client))
    monkeypatch.setattr(m,"run_current_inventory_completeness_probe",cert)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        attempt=await executor.submit_once(amendment,permit=canary_permit())
        assert attempt.journal_state=="SUBMITTING"
        assert journal.rows[amendment.external_id]=="SUBMITTING"
        assert len(client.posts)==1
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=canary_permit())
    assert len(client.posts)==1


@pytest.mark.asyncio
async def test_ambiguous_post_marks_unknown_and_never_retries(monkeypatch):
    client=Client()
    async def ambiguous(path,body):
        client.posts.append((path,body))
        raise TimeoutError("EXCHANGE OUTCOME UNKNOWN WITH PRIVATE URL")
    client._private_post=ambiguous
    journal=MemoryJournal()
    lifecycle=StopAmendmentLifecycleCoordinator(journal)
    amendment,_=await lifecycle.prepare(preflight().proposal)
    executor=StopMutationExecutor(lifecycle=lifecycle,transport=transport(client))
    monkeypatch.setattr(m,"run_current_inventory_completeness_probe",cert)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=canary_permit())
        assert journal.rows[amendment.external_id]=="UNKNOWN"
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=canary_permit())
    assert len(client.posts)==1
