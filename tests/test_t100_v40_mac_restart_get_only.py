"""No-network, no-trade tests for isolated T100 pilot process restart drill."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.t100_v40_mac_restart_get_only as m
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult


def sample(**overrides):
    data=asdict(PilotGetOnlyResult(
        status=m.GOOD,
        samples=2,
        position_count=0,
        native_stop_count=0,
        account_equity_usdt="58.86532847",
        available_balance_usdt="58.86532847988",
        hedge_mode_observed=True,
        inventory_bookends_equal=True,
        available_equity_roundoff_observed=True,
    ))
    data.update(overrides)
    return data


def test_clean_two_independent_process_results_do_not_authorize_live():
    out=m.assess_separate_processes(sample(),sample())
    assert out.status==m.READY
    assert out.separate_python_processes_observed==2
    assert out.signed_inventory_samples_observed==4
    assert out.flat_account_stable_across_processes
    assert out.hedge_mode_verified
    assert out.open_positions==0
    assert out.active_stops==0
    assert out.account_equity_usdt=="58.86532847"
    assert out.available_balance_usdt=="58.86532847988"
    assert out.independent_nonflat_checkpoint_restart_verified is False
    assert out.full_position_native_market_stop_verified_in_this_drill is False
    assert out.continuous_stop_amendment_protection_verified is False
    assert out.certified_api_trading_permission is False
    assert out.independently_proven_subaccount_identity is False
    assert out.exchange_atomicity_certified is False
    assert out.live_authorization is False
    assert out.entry_allowed is False
    assert out.mexc_posts==0
    assert out.database_writes==0


@pytest.mark.parametrize("bad",[
    None,[],{},{"status":m.GOOD},
])
def test_nonconforming_child_never_passes(bad):
    result=m.assess_separate_processes(sample(),bad)
    assert result.status.endswith("_HALT")
    assert not result.entry_allowed


@pytest.mark.parametrize("field,value",[
    ("status","PILOT_OPEN_POSITIONS_PRESENT_HALT"),
    ("samples",1),("samples",True),
    ("position_count",1),("position_count",True),
    ("native_stop_count",1),("native_stop_count",True),
    ("hedge_mode_observed",False),
    ("inventory_bookends_equal",False),
    ("mexc_posts",1),("mexc_posts",True),
    ("db_mutations",1),("db_mutations",True),
    ("live_authorization",True),
    ("private_post_permission_verified",True),
    ("exact_native_stop_lifecycle_verified",True),
    ("account_scope_independently_verified",True),
    ("cross_margin_collateral_verified",True),
])
def test_child_safety_drift_rejected(field,value):
    out=m.assess_separate_processes(sample(),sample(**{field:value}))
    assert out.status.endswith("_HALT")
    assert out.mexc_posts==0
    assert out.database_writes==0


@pytest.mark.parametrize("field,value",[
    ("account_equity_usdt","0"),
    ("account_equity_usdt","NaN"),
    ("account_equity_usdt","a-private-access-token"),
    ("account_equity_usdt",None),
    ("available_balance_usdt","Infinity"),
    ("available_balance_usdt","58.866"),
    ("available_balance_usdt","-1"),
])
def test_bad_or_inconsistent_balances_rejected_without_echo(field,value):
    out=m.assess_separate_processes(sample(),sample(**{field:value}))
    assert out.status.endswith("_HALT")
    assert out.account_equity_usdt is None
    assert out.available_balance_usdt is None
    assert "private-access-token" not in out.status


def test_different_valid_equity_between_processes_freezes():
    out=m.assess_separate_processes(
        sample(),sample(
            account_equity_usdt="60.00000000",
            available_balance_usdt="60.00000000988",
        ),
    )
    assert out.status=="PILOT_RESTART_EQUITY_CHANGED_BETWEEN_PROCESSES_HALT"


@pytest.mark.parametrize("flag,value",[
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
])
def test_wrong_controls_abort_before_any_child(monkeypatch,flag,value):
    monkeypatch.setattr(m.sys,"platform","darwin")
    def forbidden(*args,**kwargs):
        raise AssertionError("NO_CHILD_BEFORE_PAPER_KILL")
    monkeypatch.setattr(m,"run_child",forbidden)
    env={**m.SAFE_ENV,flag:value}
    out=m.run_mac_restart_drill(
        service=m.WEB_PILOT_KEYCHAIN_SERVICE,env=env,
    )
    assert out.status=="PILOT_RESTART_MAC_PAPER_KILL_OR_SCOPE_REQUIRED_HALT"


def test_wrong_account_or_wrong_os_never_reads_keychain(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    monkeypatch.setattr(m,"run_child",lambda *_:pytest.fail("WRONG_KEYCHAIN"))
    out=m.run_mac_restart_drill(
        service="t100225-mexc-pilot-subaccount",env=dict(m.SAFE_ENV),
    )
    assert out.status=="PILOT_RESTART_MAC_PAPER_KILL_OR_SCOPE_REQUIRED_HALT"
    monkeypatch.setattr(m.sys,"platform","linux")
    out=m.run_mac_restart_drill(
        service=m.WEB_PILOT_KEYCHAIN_SERVICE,env=dict(m.SAFE_ENV),
    )
    assert out.status=="PILOT_RESTART_MAC_PAPER_KILL_OR_SCOPE_REQUIRED_HALT"


def test_true_separate_child_calls_sanitize_environment(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    calls=[]
    def child(service,path,env):
        calls.append((service,path,env))
        assert env=={
            **m.SAFE_ENV,
            "HOME":"/tmp/test-home","PATH":"/usr/bin",
        }
        assert "MEXC_API_KEY" not in env
        assert "MEXC_API_SECRET" not in env
        assert "DATABASE_URL" not in env
        assert "BOXWAVE_SECRET" not in env
        return sample()
    monkeypatch.setattr(m,"run_child",child)
    env={
        **m.SAFE_ENV,"HOME":"/tmp/test-home","PATH":"/usr/bin",
        "MEXC_API_KEY":"SHOULD_NOT_LEAK",
        "MEXC_API_SECRET":"SHOULD_NOT_LEAK",
        "DATABASE_URL":"SHOULD_NOT_LEAK",
        "BOXWAVE_SECRET":"SHOULD_NOT_LEAK",
    }
    out=m.run_mac_restart_drill(
        service=m.WEB_PILOT_KEYCHAIN_SERVICE,env=env,
    )
    assert out.status==m.READY
    assert len(calls)==2
    assert calls[0][1]!=calls[1][1]
    assert calls[0][1].name=="first.json"
    assert calls[1][1].name=="second.json"


def test_first_child_failure_does_not_trigger_second(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    calls=[]
    def failing(service,path,env):
        calls.append(path)
        return None
    monkeypatch.setattr(m,"run_child",failing)
    out=m.run_mac_restart_drill(
        service=m.WEB_PILOT_KEYCHAIN_SERVICE,env=dict(m.SAFE_ENV),
    )
    assert out.status=="PILOT_RESTART_FIRST_PROCESS_FAILED_HALT"
    assert len(calls)==1
    assert not out.live_authorization


def test_second_child_failure_freezes_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    calls=[]
    def child(service,path,env):
        calls.append(path)
        return sample() if len(calls)==1 else None
    monkeypatch.setattr(m,"run_child",child)
    out=m.run_mac_restart_drill(
        service=m.WEB_PILOT_KEYCHAIN_SERVICE,env=dict(m.SAFE_ENV),
    )
    assert out.status=="PILOT_RESTART_SECOND_PROCESS_FAILED_HALT"
    assert len(calls)==2
    assert out.database_writes==0


def test_real_child_uses_fixed_module_not_shell_and_never_returns_raw_stdout(
    monkeypatch,tmp_path,
):
    calls=[]
    def process(command,**kwargs):
        calls.append((command,kwargs))
        report=Path(command[-1])
        report.write_text(__import__("json").dumps(sample()))
        return SimpleNamespace(returncode=0,stdout=b"PRIVATE_NOT_RETURNED",stderr=b"")
    monkeypatch.setattr(m.subprocess,"run",process)
    out=m.run_child(
        m.WEB_PILOT_KEYCHAIN_SERVICE,tmp_path/"child.json",
        {**m.SAFE_ENV,"HOME":"/tmp"},
    )
    assert out==sample()
    assert calls[0][0][:3]==(m.sys.executable,"-m",m.MODULE)
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["check"] is False
    assert "shell" not in calls[0][1]
    assert "PRIVATE_NOT_RETURNED" not in str(out)
