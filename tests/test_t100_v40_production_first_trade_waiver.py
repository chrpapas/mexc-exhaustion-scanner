"""One first V40 real signal is the venue integration experiment, not a fake certificate."""
from dataclasses import replace
from decimal import Decimal as D
import pytest

from app.t100_live_authorization import (
    V40FirstCanaryReadinessEvidence,
    LiveAuthorizationHalted,
    issue_v40_supervised_canary_permit,
    issue_live_mutation_permit,
    LivePilotReadinessEvidence,
    require_certified_transport_permit,
)
from app.t100_live_guards import T100ExecutionControls,LIVE_CONFIRMATION


def live_controls():
    return T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"12.50",
        "T100_LIVE_LEVERAGE":"1",
    })


def genuine_trial():
    return V40FirstCanaryReadinessEvidence(
        code_revision_verified=True,
        frozen_strategy_parity_verified=True,
        immutable_journal_schema_and_grants_verified=True,
        genuine_fresh_one_signal_verified=True,
        current_exact_lot_and_depth_verified=True,
        dedicated_account_flat_and_hedge_verified=True,
        private_short_cross_one_x_verified=True,
        account_open_orders_and_unknown_claims_absent=True,
        native_attached_market_stop_request_contract_reviewed=True,
        post_fill_get_and_journal_reconciliation_reviewed=True,
        independent_nonflat_guardian_live_verified=False,
        durable_incident_alert_delivery_verified=False,
        supervised_emergency_exit_runbook_verified=True,
        operator_explicitly_authorized_first_canary=True,
        production_first_trade_observation_waiver=True,
    )


def test_user_waived_only_prior_live_behavior_but_honest_bool_fields_remain_false():
    e=genuine_trial()
    assert not e.independent_nonflat_guardian_live_verified
    assert not e.durable_incident_alert_delivery_verified
    assert e.failures()==()
    permit=issue_v40_supervised_canary_permit(
        live_controls(),e,signal_id=727,symbol="DOGE_USDT",
    )
    assert permit.scope=="T100_V40_SUPERVISED_FIRST_SIGNAL"
    assert permit.symbol=="DOGE_USDT" and permit.signal_id==727
    assert permit.max_position_notional_usdt==D("12.50")
    assert require_certified_transport_permit(permit) is permit


@pytest.mark.parametrize("lost",[
    "code_revision_verified",
    "frozen_strategy_parity_verified",
    "immutable_journal_schema_and_grants_verified",
    "genuine_fresh_one_signal_verified",
    "current_exact_lot_and_depth_verified",
    "dedicated_account_flat_and_hedge_verified",
    "private_short_cross_one_x_verified",
    "account_open_orders_and_unknown_claims_absent",
    "native_attached_market_stop_request_contract_reviewed",
    "post_fill_get_and_journal_reconciliation_reviewed",
    "supervised_emergency_exit_runbook_verified",
    "operator_explicitly_authorized_first_canary",
    "production_first_trade_observation_waiver",
])
def test_operator_cannot_waive_frozen_entry_native_stop_risk_journal_and_controls(lost):
    review=replace(genuine_trial(),**{lost:False})
    assert review.failures()
    with pytest.raises(LiveAuthorizationHalted):
        issue_v40_supervised_canary_permit(
            live_controls(),review,signal_id=727,symbol="DOGE_USDT",
        )


@pytest.mark.parametrize("control",[
    {"T100_EXECUTION_MODE":"paper"},
    {"T100_LIVE_ORDER_API_ENABLED":"false"},
    {"T100_LIVE_KILL_SWITCH":"true"},
    {"T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.51"},
    {"T100_LIVE_LEVERAGE":"2"},
    {"T100_LIVE_OPERATOR_CONFIRM":""},
])
def test_trial_never_overrides_stable_exact_risk_live_controls(control):
    env={
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"12.50",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_LEVERAGE":"1",
    }
    env.update(control)
    with pytest.raises(LiveAuthorizationHalted):
        issue_v40_supervised_canary_permit(
            T100ExecutionControls.from_env(env),
            genuine_trial(),signal_id=727,symbol="DOGE_USDT",
        )


def test_legacy_trader_not_unlocked_by_first_canary_adapter_release():
    from app.t100_live_authorization import (
        LivePilotReadinessEvidence,issue_live_mutation_permit,
    )
    ready=LivePilotReadinessEvidence(**{
        name:True for name in LivePilotReadinessEvidence.__dataclass_fields__
    })
    p=issue_live_mutation_permit(live_controls(),ready)
    assert p.scope=="T100_TINY_PILOT"
    with pytest.raises(LiveAuthorizationHalted,match="LIVE_ADAPTER_NOT_CERTIFIED"):
        require_certified_transport_permit(p)
