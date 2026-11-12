"""V40 real-signal canary releases differ from full autonomous permissions.

All tests are offline and never open MEXC connections, submit orders or
change deployment flags. Production constant stays NOT_IMPLEMENTED.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_authorization import (
    V40FirstCanaryReadinessEvidence,V40_CANARY_SCOPE,
    issue_v40_supervised_canary_permit,require_live_mutation_permit,
    require_certified_transport_permit,LiveAuthorizationHalted,
)
from app.t100_live_guards import T100ExecutionControls,LIVE_CONFIRMATION
from app import t100_live_guards


def controls(**changes):
    env=dict(
        T100_EXECUTION_MODE="live",T100_LIVE_ORDER_API_ENABLED="true",
        T100_LIVE_KILL_SWITCH="false",
        T100_LIVE_OPERATOR_CONFIRM=LIVE_CONFIRMATION,
        T100_LIVE_MAX_POSITION_NOTIONAL_USDT="12.50",
        T100_LIVE_MAX_TOTAL_NOTIONAL_USDT="12.50",
        T100_LIVE_MAX_DAILY_LOSS_USDT="12.50",
        T100_LIVE_LEVERAGE="1",
    )
    env.update(changes)
    return T100ExecutionControls.from_env(env)


def evidence(**changes):
    d={name:True for name in V40FirstCanaryReadinessEvidence.__dataclass_fields__}
    d.update(changes)
    return V40FirstCanaryReadinessEvidence(**d)


def issue(**args):
    return issue_v40_supervised_canary_permit(
        args.get("controls",controls()),args.get("evidence",evidence()),
        signal_id=args.get("signal_id",496),
        symbol=args.get("symbol","EXAMPLE_USDT"),
    )


def test_first_pilot_can_be_permitted_without_pretending_postfill_soak():
    permit=issue()
    assert permit.scope==V40_CANARY_SCOPE
    assert permit.signal_id==496 and permit.symbol=="EXAMPLE_USDT"
    assert permit.max_position_notional_usdt==D("12.50")
    assert permit.max_total_notional_usdt==D("12.50")
    assert permit.max_daily_loss_usdt==D("12.50")
    assert require_live_mutation_permit(permit) is permit
    # Production has not certified the live adapter; no trade is possible.
    with pytest.raises(LiveAuthorizationHalted,match="CANARY_ADAPTER_NOT_RELEASED"):
        require_certified_transport_permit(permit)


def test_first_pilot_does_not_unblock_legacy_tiny_pilot_globally(monkeypatch):
    permit=issue()
    monkeypatch.setattr(t100_live_guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    assert require_certified_transport_permit(permit) is permit
    from tests.test_t100_live_authorization import controls as old_controls,evidence as old_evidence
    from app.t100_live_authorization import issue_live_mutation_permit
    legacy=issue_live_mutation_permit(old_controls(),old_evidence())
    with pytest.raises(LiveAuthorizationHalted,match="LIVE_ADAPTER_NOT_CERTIFIED"):
        require_certified_transport_permit(legacy)


@pytest.mark.parametrize("field",tuple(V40FirstCanaryReadinessEvidence.__dataclass_fields__))
def test_independent_guardian_native_stop_and_each_prerequisite_mandatory(field):
    with pytest.raises(LiveAuthorizationHalted,match="V40_CANARY_AUTHORIZATION_BLOCKED"):
        issue(evidence=evidence(**{field:False}))


@pytest.mark.parametrize("change",[
    {"T100_LIVE_ORDER_API_ENABLED":"false"},
    {"T100_LIVE_KILL_SWITCH":"true"},
    {"T100_LIVE_OPERATOR_CONFIRM":""},
    {"T100_EXECUTION_MODE":"paper"},
    {"T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.500000001"},
    {"T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"13"},
    {"T100_LIVE_MAX_DAILY_LOSS_USDT":"13"},
    {"T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"0"},
    {"T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":""},
    {"T100_LIVE_LEVERAGE":"2"},
])
def test_pilot_switch_and_strict_financial_caps(change):
    # The base controls parser rejects invalid decimal inputs before the
    # additional supervised-canary authorization boundary is reached.
    with pytest.raises((LiveAuthorizationHalted,ValueError)):
        issue(controls=controls(**change))


@pytest.mark.parametrize("signal_id,symbol",[
    (True,"EXAMPLE_USDT"),(0,"EXAMPLE_USDT"),
    (496,"BTC_USDT"),(496,"ETH_USDT"),
    (496,"../EXAMPLE_USDT"),(496,"example_usdt"),
    (496,""),(496,None),
])
def test_permit_requires_exact_real_stage2_identity(signal_id,symbol):
    with pytest.raises(LiveAuthorizationHalted):
        issue(signal_id=signal_id,symbol=symbol)


def test_invalid_forged_canary_economics_fails_even_if_scope_string_matches():
    # Deliberately use object.__setattr__ in offline tests to simulate
    # tampered/deserialized runtime values. This frozen typed object is
    # an accidental-use gate, not a cryptographic capability.
    for field,value in (
        ("max_position_notional_usdt",D("100")),
        ("max_total_notional_usdt",D("100")),
        ("max_daily_loss_usdt",D("100")),
        ("signal_id",0),
        ("symbol","OTHER"),
        ("leverage",2),
    ):
        bad=issue()
        object.__setattr__(bad,field,value)
        with pytest.raises(LiveAuthorizationHalted):
            require_live_mutation_permit(bad)


def test_production_adapter_remains_unimplemented():
    assert t100_live_guards.LIVE_ADAPTER_STATUS=="NOT_IMPLEMENTED"
