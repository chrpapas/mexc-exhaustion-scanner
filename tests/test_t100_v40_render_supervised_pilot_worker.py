"""No-POST tests for the genuine V40 source/account continuous supervisor."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from app.t100_v40_render_signal_intake import V40SignalIntakeReport
from app.t100_v40_render_supervised_pilot_worker import (
    V40SupervisedPilotObservation, monitor_environment,
    supervise_real_v40_cycle, run_supervised_signal_monitor,
)
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult
from scripts.t100_v40_pilot_capability_get_only import PilotCapabilityReport
from app.t100_v40_render_journal_readonly import V40RuntimeJournalObservation

BASE={
    "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
    "T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED":"true",
    "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "T100_V40_SIGNAL_READER_DSN":"postgresql://dedicated-reader:redacted@internal-db/db",
    "T100_V40_EXECUTOR_RUNTIME_DSN":"postgresql://t100_live_executor_runtime:redacted@internal-db/db",
    "MEXC_API_KEY":"RENDER_TEST_DEDICATED_PILOT_KEY_123456",
    "MEXC_API_SECRET":"RENDER_TEST_DEDICATED_PILOT_SECRET_123456",
    "T100_V40_REAL_SIGNAL_MONITOR_ENABLED":"true",
    "T100_V40_MONITOR_SINGLE_POSITION_SCOPE":"true",
}


def flat():
    return PilotGetOnlyResult(
        status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
        samples=2,position_count=0,native_stop_count=0,
        account_equity_usdt="56.37671495",
        available_balance_usdt="56.37671495",
        hedge_mode_observed=True,inventory_bookends_equal=True,
    )


def empty():
    return V40SignalIntakeReport(
        status="V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED",
        fresh_source_verified=True,
    )


def candidate():
    return V40SignalIntakeReport(
        status="V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
        signal_count=1,signal_id=492,symbol="TEST_USDT",
        tier="HIGH_225",pre24_vol_pct=8.,
        fresh_source_verified=True,current_flat_account_verified=True,
        account_equity_usdt="56.37671495",
        theoretical_v40_notional_usdt="12.50",
    )


def capability():
    return PilotCapabilityReport(
        status="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
        symbol="TEST_USDT",flat_hedge_account_bookended=True,
        public_futures_contract_eligible_observed=True,
        current_private_short_cross_1x_tier_observed=True,
        one_contract_on_public_lot_grid=True,
        current_short_tier_supports_one_contract=True,
        short_margin_mode_observed="CROSS",short_leverage_observed=1,
    )


def journal():
    return V40RuntimeJournalObservation(
        "V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED",
        runtime_role_verified=True,
        repeatable_read_readonly_verified=True,
        v40_tables_observed=True,
        v40_dispatch_state_unresolved_absent=True,
        v40_no_prior_claims_or_slots=True,
        legacy_unresolved_absent=True,
        first_position_journal_clear=True,
    )


def run(env=BASE,source=None,account=None,cap=None,journal_result=None):
    calls=[]
    async def source_read(environment):
        calls.append("source")
        return empty() if source is None else source
    async def journal_observe(environment):
        calls.append("journal")
        if isinstance(journal_result,BaseException):
            raise journal_result
        return journal() if journal_result is None else journal_result
    async def account_observe(*,key,secret):
        calls.append("account")
        assert key==BASE["MEXC_API_KEY"]
        assert secret==BASE["MEXC_API_SECRET"]
        if isinstance(account,BaseException):
            raise account
        return flat() if account is None else account
    async def capability_observe(*,key,secret,symbol):
        calls.append("capability")
        assert key==BASE["MEXC_API_KEY"] and secret==BASE["MEXC_API_SECRET"]
        assert symbol=="TEST_USDT"
        if isinstance(cap,BaseException):
            raise cap
        return capability() if cap is None else cap
    result=asyncio.run(supervise_real_v40_cycle(
        env,signal_read=source_read,account_observe=account_observe,
        capability_observe=capability_observe,
        journal_observe=journal_observe,
    ))
    return result,calls


def test_empty_genuine_scanner_continuously_gets_flat_account():
    result,calls=run()
    assert calls==["source","journal","account"]
    assert result.status=="V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT"
    assert result.source_verified and result.account_flat_verified
    assert result.signal_id is None and result.fresh_signal_count==0
    assert result.account_equity_usdt=="56.37671495"
    assert not result.live_authorization
    assert not result.entry_allowed and not result.live_adapter_connected
    assert result.mexc_orders_sent==0 and result.database_writes==0


def test_real_fresh_v40_candidate_is_logged_not_executed():
    result,calls=run(source=candidate())
    assert calls==["source","journal","account","capability"]
    assert result.status=="V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED"
    assert result.signal_id==492 and result.signal_symbol=="TEST_USDT"
    assert result.real_signal_economics_observed
    assert result.candidate_public_contract_and_cross_1x_verified is True
    assert result.candidate_exact_strategy_lot_verified is False
    assert result.candidate_capability_status=="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING"
    assert result.enabled_position_capacity==0
    assert not result.native_stop_management_connected
    assert not result.continuous_nonflat_guardian_connected
    assert not result.live_authorization
    assert not result.entry_allowed


@pytest.mark.parametrize("flag,value",[
    ("T100_V40_REAL_SIGNAL_MONITOR_ENABLED","false"),
    ("T100_V40_MONITOR_SINGLE_POSITION_SCOPE","false"),
    ("T100_V40_EXECUTOR_DISABLED_WORKER","false"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("TRADING_MODE","live"),
    ("EXECUTION_ENABLED","true"),
    ("T100_V40_SIGNAL_READER_DSN",""),
    ("DATABASE_URL","postgres://owner:SECRET@db/database"),
])
def test_misconfigured_flags_reject_before_any_network_or_db(flag,value):
    env={**BASE,flag:value}
    result,calls=run(env)
    assert not monitor_environment(env)
    assert result.status=="V40_REAL_SIGNAL_MONITOR_PAPER_KILL_OR_SCOPE_REQUIRED"
    assert calls==[]


@pytest.mark.parametrize("source",[
    replace(empty(),fresh_source_verified=False),
    replace(empty(),status="V40_SOURCE_READ_ONLY_POSTGRES_QUERY_FAILED"),
    replace(empty(),mexc_posts=1),
    replace(candidate(),live_authorization=True),
    replace(candidate(),theoretical_v40_notional_usdt=None),
    replace(candidate(),signal_count=2),
])
def test_unknown_source_or_unapproved_signal_halts_without_account_get(source):
    result,calls=run(source=source)
    assert result.status.startswith("V40_REAL_SIGNAL_")
    assert result.status.endswith("_HALT")
    assert calls==["source"]


@pytest.mark.parametrize("account",[
    replace(flat(),position_count=1),
    replace(flat(),native_stop_count=1),
    replace(flat(),samples=1),
    replace(flat(),hedge_mode_observed=False),
    replace(flat(),inventory_bookends_equal=False),
    replace(flat(),mexc_posts=1),
    ValueError("PRIVATE_ACCOUNT_PASSWORD_AND_RAW_ERROR"),
])
def test_exchange_nonflat_unverified_or_failed_halts_without_secret_leak(account):
    result,calls=run(account=account)
    assert result.status in (
        "V40_REAL_SIGNAL_ACCOUNT_INVENTORY_UNVERIFIED_HALT",
        "V40_REAL_SIGNAL_ACCOUNT_NOT_FLAT_OR_GET_FAILED_HALT",
    )
    assert calls==["source","journal","account"]
    assert "PRIVATE_ACCOUNT_PASSWORD" not in str(result)
    assert BASE["MEXC_API_SECRET"] not in str(result)


def test_signals_not_acted_on_when_account_equity_drift():
    result,calls=run(source=candidate(),account=replace(flat(),account_equity_usdt="55"))
    assert result.status=="V40_REAL_SIGNAL_ACCOUNT_EQUITY_DRIFT_HALT"
    assert calls==["source","account"]


def test_supervised_monitor_cycles_then_exits_without_orders(capsys):
    events=[]
    async def observe(_env):
        events.append("cycle")
        return V40SupervisedPilotObservation(
            status="V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT",
            source_verified=True,account_flat_verified=True,
        )
    async def delay(_seconds):events.append("sleep")
    code=asyncio.run(run_supervised_signal_monitor(
        BASE,max_cycles=3,observe_cycle=observe,delay=delay,
    ))
    assert code==0 and events==[
        "cycle","sleep","cycle","sleep","cycle",
    ]
    printed=capsys.readouterr().out
    assert "V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT" in printed
    assert "live_authorization" in printed
    assert "PASSWORD" not in printed


def test_supervisor_halts_after_unverified_first_cycle(capsys):
    events=[]
    async def observe(_env):
        events.append("cycle")
        return V40SupervisedPilotObservation(
            status="V40_REAL_SIGNAL_ACCOUNT_INVENTORY_UNVERIFIED_HALT",
        )
    async def delay(_seconds):
        raise AssertionError("must never sleep after halt")
    code=asyncio.run(run_supervised_signal_monitor(
        BASE,max_cycles=10,observe_cycle=observe,delay=delay,
    ))
    assert code==2 and events==["cycle"]
    assert "UNVERIFIED_HALT" in capsys.readouterr().out


@pytest.mark.parametrize("cap_result",[
    replace(capability(),short_margin_mode_observed="ISOLATED"),
    replace(capability(),short_leverage_observed=2),
    replace(capability(),current_short_tier_supports_one_contract=False),
    replace(capability(),one_contract_on_public_lot_grid=False),
    replace(capability(),status="PILOT_PUBLIC_CONTRACT_NOT_ELIGIBLE_HALT"),
    replace(capability(),symbol="OTHER_USDT"),
    replace(capability(),mexc_posts=1),
    RuntimeError("PRIVATE_API_SECRET_IN_ERROR_MESSAGE"),
])
def test_live_candidate_cross_or_contract_unverified_halts_without_order(cap_result):
    out,calls=run(source=candidate(),cap=cap_result)
    assert out.status in (
        "V40_REAL_SIGNAL_PRIVATE_CROSS_CAPABILITY_UNVERIFIED_HALT",
        "V40_REAL_SIGNAL_PRIVATE_CROSS_CAPABILITY_GET_FAILED_HALT",
    )
    assert calls==["source","journal","account","capability"]
    assert not out.live_authorization and not out.entry_allowed
    assert "PRIVATE_API_SECRET" not in str(out)


def test_no_candidate_does_not_hit_public_contract_or_risk_api():
    out,calls=run(source=empty())
    assert out.status=="V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT"
    assert calls==["source","account"]
    assert not out.candidate_public_contract_and_cross_1x_verified
    assert not out.candidate_exact_strategy_lot_verified


@pytest.mark.parametrize("journal_result",[
    replace(journal(),first_position_journal_clear=False),
    replace(journal(),legacy_unresolved_absent=False),
    replace(journal(),runtime_role_verified=False),
    replace(journal(),database_writes=1),
    RuntimeError("DB_PASSWORD_NEVER_LEAK"),
])
def test_candidate_journal_ambiguity_halts_before_mexc_account(journal_result):
    result,calls=run(source=candidate(),journal_result=journal_result)
    assert result.status in (
        "V40_REAL_SIGNAL_RUNTIME_JOURNAL_UNRESOLVED_HALT",
        "V40_REAL_SIGNAL_RUNTIME_JOURNAL_READ_FAILED_HALT",
    )
    assert calls==["source","journal"]
    assert "DB_PASSWORD" not in str(result)
    assert not result.entry_allowed and not result.live_authorization


def test_candidate_requires_separate_restricted_runtime_dsn():
    env=dict(BASE)
    del env["T100_V40_EXECUTOR_RUNTIME_DSN"]
    result,calls=run(env=env,source=candidate())
    assert result.status=="V40_REAL_SIGNAL_RUNTIME_JOURNAL_DSN_REQUIRED_HALT"
    assert calls==["source"]
