"""Offline-only guarded handoff tests. Never MEXC POST or PostgreSQL mutation."""
import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.t100_v40_live_signal_handoff as m
from app.t100_live_authorization import LiveAuthorizationHalted
from app.t100_live_execution import LiveCaps
from app.t100_live_v40_entry_coordinator import V40OneShotReport
from app.t100_v40_render_supervised_pilot_worker import V40SupervisedPilotObservation


def candidate_observation(**changes):
    base = V40SupervisedPilotObservation(
        status=m.FRESH_SIGNAL_STATUS,
        source_status="V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
        signal_id=42,
        signal_symbol="EXAMPLE_USDT",
        signal_tier="LOW_100",
        theoretical_notional_usdt="10",
        fresh_signal_count=1,
        account_equity_usdt="56.37671495",
        account_flat_verified=True,
        source_verified=True,
        real_signal_economics_observed=True,
        candidate_public_contract_and_cross_1x_verified=True,
        runtime_journal_status=m.FIRST_JOURNAL_STATUS,
        first_position_journal_clean=True,
        enabled_position_capacity=1,
        live_adapter_connected=True,
        native_stop_management_connected=True,
        continuous_nonflat_guardian_connected=True,
    )
    return replace(base, **changes)


def tiny_caps():
    return LiveCaps(
        max_order_usdt=Decimal("12.50"),
        max_portfolio_usdt=Decimal("12.50"),
        max_daily_loss_usdt=Decimal("5"),
    )


def fake_permit():
    # Not a real permit! Tests monkeypatch the independent transport gate.
    return SimpleNamespace(
        max_position_notional_usdt=Decimal("12.50"),
        max_total_notional_usdt=Decimal("12.50"),
        leverage=1,
        scope="T100_TINY_PILOT",
    )


class CoordinatorSpy:
    def __init__(self, fail=False):
        self.calls=[]
        self.fail=fail

    async def run_one(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("SENSITIVE EXCHANGE ERROR")
        return V40OneShotReport(
            "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
            signal_id=kwargs["monitored_signal_id"],
            reserved=True,
            staged=True,
            post_attempted=True,
            claim_may_exist=True,
            independently_recovered_after_commit=True,
        )


def invoke(spy, **kw):
    return asyncio.run(m.run_monitored_real_signal_once(
        observation=kw.get("observation",candidate_observation()),
        coordinator=spy,
        permit=kw.get("permit",fake_permit()),
        caps=kw.get("caps",tiny_caps()),
        parity_report=kw.get("parity_report",{}),
        holder_id=kw.get("holder_id","fenced-executor"),
    ))


def test_actual_production_adapter_guard_blocks_before_coordinator():
    spy=CoordinatorSpy()
    with pytest.raises(LiveAuthorizationHalted):
        invoke(spy,permit=None)
    assert spy.calls==[]


@pytest.mark.parametrize("fields,expected",[
    ({"fresh_signal_count":0},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"signal_id":None},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"signal_tier":"CHANGED"},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"source_verified":False},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"account_flat_verified":False},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"runtime_journal_status":None},"V40_HANDOFF_FRESH_SIGNAL_OR_JOURNAL_UNVERIFIED"),
    ({"native_stop_management_connected":False},"V40_HANDOFF_NATIVE_STOP_GUARDIAN_OR_ADAPTER_NOT_CONNECTED"),
    ({"continuous_nonflat_guardian_connected":False},"V40_HANDOFF_NATIVE_STOP_GUARDIAN_OR_ADAPTER_NOT_CONNECTED"),
    ({"enabled_position_capacity":0},"V40_HANDOFF_NATIVE_STOP_GUARDIAN_OR_ADAPTER_NOT_CONNECTED"),
    ({"live_adapter_connected":False},"V40_HANDOFF_NATIVE_STOP_GUARDIAN_OR_ADAPTER_NOT_CONNECTED"),
    ({"mexc_orders_sent":1},"V40_HANDOFF_MONITOR_MUTATION_OR_AUTHORITY_VIOLATION"),
    ({"account_equity_usdt":"bad"},"V40_HANDOFF_EQUITY_UNVERIFIED"),
    ({"theoretical_notional_usdt":None},"V40_HANDOFF_STRATEGY_SIZE_UNVERIFIED"),
])
def test_all_uncertified_or_modified_monitor_states_are_refused(
    monkeypatch,fields,expected,
):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda _:None)
    spy=CoordinatorSpy()
    result=invoke(spy,observation=candidate_observation(**fields))
    assert result.status==expected
    assert result.post_attempted is False
    assert spy.calls==[]


def test_budget_above_one_position_ceiling_fails_before_coordinator(monkeypatch):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda _:None)
    spy=CoordinatorSpy()
    too_large=LiveCaps(
        max_order_usdt=Decimal("20"),
        max_portfolio_usdt=Decimal("20"),
        max_daily_loss_usdt=Decimal("5"),
    )
    result=invoke(spy,caps=too_large)
    assert result.status=="V40_HANDOFF_PILOT_BUDGET_OR_CROSS_LIMIT_EXCEEDED"
    assert spy.calls==[]


def test_bounded_handoff_pins_exact_genuine_signal_in_injected_coordinator(monkeypatch):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda _:None)
    spy=CoordinatorSpy()
    result=invoke(spy)
    assert result.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
    assert len(spy.calls)==1
    assert spy.calls[0]["monitored_signal_id"]==42
    assert spy.calls[0]["monitored_symbol"]=="EXAMPLE_USDT"
    assert spy.calls[0]["monitored_tier"]=="LOW_100"
    assert spy.calls[0]["holder_id"]=="fenced-executor"
    assert spy.calls[0]["caps"].max_order_usdt==Decimal("12.50")


def test_unknown_coordinator_outcome_freezes_without_retry_or_sensitive_error(monkeypatch):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda _:None)
    spy=CoordinatorSpy(fail=True)
    result=invoke(spy)
    assert result.status=="V40_HANDOFF_COORDINATOR_EXCEPTION_MANUAL_RECOVERY"
    assert result.claim_may_exist is True
    assert result.post_attempted is False
    assert len(spy.calls)==1
    assert "SENSITIVE" not in str(result)
