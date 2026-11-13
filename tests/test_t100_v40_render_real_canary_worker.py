"""V40 real worker cannot be armed by environment flags or GET-only success."""
import asyncio
from dataclasses import asdict,replace
from unittest.mock import patch

import pytest

import app.t100_v40_render_real_canary_worker as m
import app.t100_live_guards as guards


def valid_env():
    return {
        m.FLAG:"true","T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
        "T100_V40_SINGLE_POSITION_PILOT":"true",
        "T100_V40_EMERGENCY_EXIT_ENABLED":"true",
        "TRADING_MODE":"live","EXECUTION_ENABLED":"true",
        "T100_V40_EXECUTOR_DISABLED_WORKER":"false",
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":guards.LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"12.50",
        "T100_LIVE_LEVERAGE":"1",
        m.SOURCE_DSN:"postgresql://signal-reader.invalid/mexc_exhaustion",
        m.RUNTIME_DSN:"postgresql://executor-runtime.invalid/mexc_exhaustion",
        "MEXC_API_KEY":"EXAMPLE_NO_REAL_KEY_12345",
        "MEXC_API_SECRET":"EXAMPLE_NO_REAL_SECRET_123456789",
    }


def approved():
    return m.V40FirstCanaryReleaseReview(
        code_revision_verified=True,
        native_attached_market_stop_request_contract_reviewed=True,
        post_fill_get_and_journal_reconciliation_reviewed=True,
        independent_nonflat_guardian_live_verified=True,
        durable_incident_alert_delivery_verified=True,
        supervised_emergency_exit_runbook_verified=True,
        operator_explicitly_authorized_first_canary=True,
    )


def test_production_never_inferrs_native_stop_or_emergency_release_from_live_flags():
    r=m.validate_live_worker_config(valid_env())
    assert r.status=="V40_REAL_WORKER_NATIVE_STOP_OR_RECOVERY_RELEASE_NOT_CERTIFIED"
    assert not r.release_review_complete
    assert not r.live_authorization and not r.entry_allowed
    assert not r.further_order_allowed
    assert not r.order_post_may_have_occurred


@pytest.mark.parametrize("change,expected",[
    ({m.FLAG:"false"},"V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT"),
    ({"T100_V40_ACCOUNT_PROFILE":"BOXWAVE"},
     "V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT"),
    ({"T100_V40_SINGLE_POSITION_PILOT":"false"},
     "V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT"),
    ({"T100_V40_EMERGENCY_EXIT_ENABLED":"false"},
     "V40_REAL_WORKER_EMERGENCY_CLOSE_WATCH_REQUIRED"),
    ({"DATABASE_URL":"postgresql://owner.invalid/private"},
     "V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT"),
    ({"T100_V40_EXECUTOR_DISABLED_WORKER":"true"},
     "V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT"),
    ({"T100_EXECUTION_MODE":"paper"},
     "V40_REAL_WORKER_TINY_PILOT_CONTROLS_MISSING"),
    ({"T100_LIVE_ORDER_API_ENABLED":"false"},
     "V40_REAL_WORKER_TINY_PILOT_CONTROLS_MISSING"),
    ({"T100_LIVE_KILL_SWITCH":"true"},
     "V40_REAL_WORKER_TINY_PILOT_CONTROLS_MISSING"),
    ({"T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.51"},
     "V40_REAL_WORKER_TINY_PILOT_CONTROLS_MISSING"),
    ({m.SOURCE_DSN:""},
     "V40_REAL_WORKER_DATABASE_ROLE_DSN_MISSING"),
    ({m.RUNTIME_DSN:""},
     "V40_REAL_WORKER_DATABASE_ROLE_DSN_MISSING"),
    ({"MEXC_API_KEY":""},
     "V40_REAL_WORKER_PILOT_ACCOUNT_KEY_MISSING"),
])
def test_every_wrong_venue_role_or_limit_halts_before_import_or_io(change,expected):
    env=valid_env()
    env.update(change)
    report=m.validate_live_worker_config(env,approved())
    assert report.status==expected
    assert not report.release_review_complete
    assert not report.entry_allowed


def test_separate_venue_key_and_runtime_sql_role_mandatory():
    env=valid_env()
    env[m.RUNTIME_DSN]=env[m.SOURCE_DSN]
    report=m.validate_live_worker_config(env,approved())
    assert report.status=="V40_REAL_WORKER_SQL_ROLES_NOT_SEPARATED"


def test_unreviewed_adapter_does_not_become_certified_by_approvals():
    r=m.validate_live_worker_config(valid_env(),approved())
    assert r.status=="V40_REAL_WORKER_EXCHANGE_ADAPTER_NOT_CERTIFIED"
    assert not r.release_review_complete


def test_only_in_offline_fixture_can_fully_reviewed_native_adapter_reach_io_gate(monkeypatch):
    monkeypatch.setattr(guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    r=m.validate_live_worker_config(valid_env(),approved())
    assert r.status=="V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE"
    assert r.release_review_complete
    assert not r.entry_allowed and not r.live_authorization


def test_missing_release_never_opens_mexc_or_postgres():
    output=asyncio.run(m.run_real_first_canary_cycle(valid_env()))
    assert output.status=="V40_REAL_WORKER_NATIVE_STOP_OR_RECOVERY_RELEASE_NOT_CERTIFIED"
    assert not output.order_post_may_have_occurred
    assert not output.source_verified


def test_runner_refuses_to_poll_when_review_unapproved(capsys):
    async def trap(*args,**kwargs):
        raise AssertionError("NEVER_CALL_TRADER")
    code=asyncio.run(m.run_live_worker(
        valid_env(),max_cycles=1,cycle=trap,
    ))
    assert code==2
    assert "NATIVE_STOP_OR_RECOVERY_RELEASE_NOT_CERTIFIED" in capsys.readouterr().out


def test_worker_accepts_only_genuine_empty_signal_before_retry(monkeypatch,capsys):
    monkeypatch.setattr(guards,"LIVE_ADAPTER_STATUS","V40_CANARY_RESTRICTED")
    # Explicit test injection only. Production REVIEW_RELEASE stays incomplete.
    monkeypatch.setattr(m,"RELEASE_REVIEW",approved())
    outputs=[
        m.V40CanaryWorkerStatus(
            "V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2",
            source_verified=True,release_review_complete=True,
        ),
        m.V40CanaryWorkerStatus(
            "V40_FIRST_CANARY_UNKNOWN_AFTER_ENTRY_BOUNDARY_RECONCILE_ONLY",
            source_verified=True,release_review_complete=True,
            order_post_may_have_occurred=True,
        ),
    ]
    called=[]
    async def cycle(env):
        called.append(1)
        return outputs.pop(0)
    async def pause(t):
        assert t==m.INTERVAL_SECONDS
    # The default config review is evaluated at function definition, not
    # dynamic monkeypatch time: explicitly patch validator as an offline
    # substitute without touching any production release code.
    monkeypatch.setattr(
        m,"validate_live_worker_config",
        lambda env,review=None:m.V40CanaryWorkerStatus(
            "V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE",
            release_review_complete=True,
        ),
    )
    code=asyncio.run(m.run_live_worker(
        valid_env(),max_cycles=10,cycle=cycle,pause=pause,
    ))
    assert code==2 and len(called)==2
    assert not outputs
    assert "OUTCOME_UNKNOWN" not in capsys.readouterr().out


def test_running_service_stays_default_disabled_not_live():
    from app.t100_v40_render_executor_disabled_worker import (
        require_disabled_worker,
    )
    env={
        "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
        "EXECUTION_ENABLED":"false",
        "TRADING_MODE":"paper",
    }
    assert require_disabled_worker(env)
    assert m.validate_live_worker_config(env).release_review_complete is False


def test_verified_protected_first_entry_hands_off_to_emergency_watch(monkeypatch,capsys):
    calls=[]
    async def entry(env):
        calls.append("Genuine.Stage2.fenced.protected")
        return m.V40CanaryWorkerStatus(
            "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
            genuine_signal_observed=True,
            order_post_may_have_occurred=True,
            release_review_complete=True,
        )
    async def watch(env):
        assert env["T100_V40_EMERGENCY_EXIT_ENABLED"]=="true"
        calls.append("EMERGENCY_WATCH")
        return 0
    monkeypatch.setattr(
        m,"validate_live_worker_config",
        lambda env,review=None:m.V40CanaryWorkerStatus(
            "V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE",
            release_review_complete=True,
        ),
    )
    result=asyncio.run(m.run_live_worker(
        valid_env(),cycle=entry,emergency_watch=watch,
        max_cycles=1,
    ))
    assert result==0
    assert calls==["Genuine.Stage2.fenced.protected","EMERGENCY_WATCH"]


def test_emergency_watch_failure_never_marks_successful_live_execution(monkeypatch):
    async def entry(env):
        return m.V40CanaryWorkerStatus(
            "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
        )
    async def watch(env):
        raise RuntimeError("MEXC_PRIVATE_KEY_UNAVAILABLE")
    monkeypatch.setattr(
        m,"validate_live_worker_config",
        lambda env,review=None:m.V40CanaryWorkerStatus(
            "V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE",
            release_review_complete=True,
        ),
    )
    assert asyncio.run(m.run_live_worker(
        valid_env(),cycle=entry,emergency_watch=watch,max_cycles=1,
    ))==2
