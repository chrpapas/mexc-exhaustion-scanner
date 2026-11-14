"""No-network, no-order single-real-signal supervised canary tests."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app import t100_live_guards
from app.t100_live_execution import LiveCaps
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator,V40OneShotReport
from app.t100_live_v40_stage2_source import V40SourceBatch
from app.t100_v40_supervised_canary_runner import execute_one_real_v40_signal
from tests.test_t100_v40_supervised_canary_permit import controls,evidence
from tests.test_t100_live_v40_entry_coordinator import candidate
from datetime import datetime,timezone


def caps():
    return LiveCaps(
        max_order_usdt=D("12.50"),
        max_portfolio_usdt=D("12.50"),
        max_daily_loss_usdt=D("12.50"),
    )


class Source:
    def __init__(self,signals=None,failed=False):
        self.calls=0
        self.failed=failed
        self.signals=[candidate()] if signals is None else signals
    async def latest_candidates(self,*,observed_at):
        self.calls+=1
        assert observed_at.tzinfo is not None
        if self.failed:
            raise RuntimeError("SECRET_DSN")
        return V40SourceBatch(
            "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
            observed_at=observed_at,
            candidates=tuple(self.signals),
        )


def make_coordinator(fail=False):
    # Real TYPE but inert collaborators. The test deliberately prevents
    # the actual DB/HTTP-backed coordinator implementation from running.
    c=V40OneShotCoordinator(
        source=None,snapshots=None,witness=None,depth=object(),
        leaders=None,reservations=None,journal=None,claims=None,
        claim_reader=None,transport=None,get_venue=None,completeness=None,
        commit_writer=None,initial_pilot_only=True,
    )
    calls=[]
    async def spy(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("UNKNOWN_MEXC_POST_RESPONSE")
        return V40OneShotReport(
            "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
            signal_id=42,post_attempted=True,claim_may_exist=True,
            independently_recovered_after_commit=True,
        )
    c.run_one=spy
    return c,calls


def invoke(*,src=None,c=None,release=None,ctrl=None,cap=None):
    if src is None:src=Source()
    if c is None:c,_=make_coordinator()
    return asyncio.run(execute_one_real_v40_signal(
        source=src,coordinator=c,
        controls=ctrl if ctrl is not None else controls(),
        evidence=release if release is not None else evidence(),
        caps=cap if cap is not None else caps(),
        parity_report={},holder_id="supervised-operator",
    ))


def test_shipping_mode_blocks_before_real_coordinator_even_with_candidate():
    src=Source()
    c,calls=make_coordinator()
    out=invoke(src=src,c=c)
    assert out.status=="V40_CANARY_INDEPENDENT_RELEASE_OR_ADAPTER_NOT_READY_NO_POST"
    assert out.signal_id==42
    assert out.real_post_may_have_been_attempted is False
    assert out.additional_entries_allowed is False
    assert out.full_autonomous_mode is False
    assert len(calls)==0
    assert src.calls==1


def test_canary_gate_can_invoke_once_after_explicit_offline_fake_release(monkeypatch):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    src=Source()
    c,calls=make_coordinator()
    out=invoke(src=src,c=c)
    assert out.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
    assert out.real_post_may_have_been_attempted
    assert out.independently_recovered_after_commit
    assert not out.additional_entries_allowed and not out.full_autonomous_mode
    assert len(calls)==1
    assert src.calls==1
    p=calls[0]["permit"]
    assert p.scope=="T100_V40_SUPERVISED_FIRST_SIGNAL"
    assert p.signal_id==42 and p.symbol=="EXAMPLE_USDT"
    assert calls[0]["monitored_signal_id"]==42
    assert calls[0]["monitored_symbol"]=="EXAMPLE_USDT"
    assert calls[0]["monitored_tier"]==candidate().tier


@pytest.mark.parametrize("signals",[
    [],[candidate(),candidate()],
    [replace(candidate(),observed_age_seconds=901)],
])
def test_empty_ambiguous_or_expired_stage2_never_calls_executor(monkeypatch,signals):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    src=Source(signals)
    c,calls=make_coordinator()
    out=invoke(src=src,c=c)
    assert out.status=="V40_CANARY_NO_SINGLE_FRESH_ELIGIBLE_STAGE2_NO_POST"
    assert out.real_post_may_have_been_attempted is False
    assert calls==[]


def test_invalid_operator_or_failed_reader_never_calls_coordinator(monkeypatch):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    c,calls=make_coordinator()
    out=invoke(c=c,ctrl=controls(T100_LIVE_ORDER_API_ENABLED="false"))
    assert out.status=="V40_CANARY_OPERATOR_LIVE_CONTROLS_OFF_NO_POST"
    assert calls==[]
    out=invoke(c=c,src=Source(failed=True))
    assert out.status=="V40_CANARY_RESTRICTED_STAGE2_UNAVAILABLE_NO_POST"
    assert calls==[]
    assert "SECRET" not in str(out)


def test_guardian_evidence_missing_stops_before_coordinator(monkeypatch):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    c,calls=make_coordinator()
    out=invoke(
        c=c,
        release=evidence(independent_nonflat_guardian_live_verified=False),
    )
    assert out.status=="V40_CANARY_INDEPENDENT_RELEASE_OR_ADAPTER_NOT_READY_NO_POST"
    assert calls==[]


def test_unknown_venue_or_commit_outcome_is_manual_only_never_retried(monkeypatch):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    c,calls=make_coordinator(fail=True)
    out=invoke(c=c)
    assert out.status=="V40_CANARY_EXECUTION_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION"
    assert out.real_post_may_have_been_attempted
    assert out.automatic_retry_allowed is False
    assert len(calls)==1


def test_canary_runner_cannot_be_used_for_autonomous_four_slot_mode(monkeypatch):
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    c,calls=make_coordinator()
    c.initial_pilot_only=False
    out=invoke(c=c)
    assert out.status=="V40_CANARY_COORDINATOR_OR_PILOT_CAP_INVALID_NO_POST"
    assert calls==[]
    out=invoke(c=c,cap=LiveCaps(
        max_order_usdt=D("15"),max_portfolio_usdt=D("15"),
        max_daily_loss_usdt=D("15"),
    ))
    assert out.status=="V40_CANARY_COORDINATOR_OR_PILOT_CAP_INVALID_NO_POST"
    assert calls==[]
