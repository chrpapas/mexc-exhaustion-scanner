"""Production worker boot route and physical NO-POST/READ-ONLY isolation.

No real database, internet, exchange request or credentials are used.
The actual typed graph construction is separately exercised in
test_t100_v40_concrete_idle_portfolio_factory.
"""
from dataclasses import asdict,replace
import asyncio
import sys

import pytest

from app import t100_v40_render_connected_portfolio_worker as m
from app import t100_v40_render_executor_disabled_worker as entry


class DummySigner:
    def __init__(self):
        self.gets=[]
    async def _private_get(self,path,params=None):
        self.gets.append((path,params))
        return {"ok":True}
    async def _private_post(self,*args,**kwargs):
        raise AssertionError("Underlying live mutation must NEVER be invoked")


def configuration():
    return {
        m.FLAG:"true",
        "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
        "EXECUTION_ENABLED":"false",
        "TRADING_MODE":"paper",
        "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
        "T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED":"false",
        "T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED":"false",
        "T100_V40_EMERGENCY_EXIT_ENABLED":"false",
        m.SOURCE_DSN:"postgresql://readonly-signal:fake@127.0.0.1/db",
        m.RUNTIME_DSN:"postgresql://readonly-runtime:fake@127.0.0.1/db",
        "MEXC_API_KEY":"1234567890AB",
        "MEXC_API_SECRET":"012345678901234567890",
    }


def test_config_requires_strict_paper_disabled_and_distinct_roles():
    env=configuration()
    assert m.validate_config(env)
    for key,value in (
        (m.FLAG,"false"),
        ("T100_V40_EXECUTOR_DISABLED_WORKER","false"),
        ("T100_EXECUTION_MODE","live"),
        ("T100_LIVE_ORDER_API_ENABLED","true"),
        ("T100_LIVE_KILL_SWITCH","false"),
        ("EXECUTION_ENABLED","true"),
        ("TRADING_MODE","live"),
        ("T100_V40_ACCOUNT_PROFILE","BOXWAVE"),
        ("T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED","true"),
        ("T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED","true"),
        ("T100_V40_EMERGENCY_EXIT_ENABLED","true"),
        (m.RUNTIME_DSN,env[m.SOURCE_DSN]),
        ("MEXC_API_KEY","short"),
        ("MEXC_API_SECRET","short"),
    ):
        bad={**env,key:value}
        assert m.validate_config(bad) is False,key


@pytest.mark.asyncio
async def test_injected_signer_physically_forbids_all_mexc_posts():
    signed=DummySigner()
    client=m.V40NoPostMexcClient(signed)
    result=await client._private_get("/api/v1/private/position/open_positions")
    assert result=={"ok":True}
    assert len(signed.gets)==1
    with pytest.raises(RuntimeError,match="POST_PHYSICALLY_DISABLED"):
        await client._private_post("/api/v1/private/order/create",{})
    assert len(signed.gets)==1


@pytest.mark.asyncio
async def test_bad_config_never_imports_pool_or_mexc(monkeypatch):
    cfg=configuration()
    cfg["T100_LIVE_KILL_SWITCH"]="false"
    result=await m.observe_connected_portfolio_once(cfg)
    assert result.status=="V40_CONNECTED_PORTFOLIO_PAPER_ROLE_OR_ACCOUNT_CONFIG_HALT"
    assert result.exchange_posts==0 and result.database_writes==0
    assert not result.entry_allowed and not result.live_authorization


@pytest.mark.asyncio
async def test_loop_only_continues_verified_get_only_no_signal(monkeypatch,capsys):
    calls=[]
    good=m.V40ConnectedPortfolioReport(
        "V40_CONNECTED_LIFECYCLE_PORTS_GET_ONLY_V40_SUPERVISOR_WAIT_GENUINE_STAGE2",
        source_connected=True,guardian_connected=True,
        account_inventory_verified=True,complete_exit_ports_constructed=True,
        native_stop_management_port_constructed=True,
        financial_settlement_port_constructed=True,
        supervisor_status="V40_SUPERVISOR_WAIT_GENUINE_STAGE2",
        source_candidate_count=0,observed_slots=0,
    )
    async def once(_env):
        calls.append("GET_ACCOUNT_AND_REAL_PORTS")
        return good
    monkeypatch.setattr(m,"observe_connected_portfolio_once",once)
    assert await m.run_connected_monitor(configuration(),max_cycles=1)==0
    assert calls==["GET_ACCOUNT_AND_REAL_PORTS"]
    assert "live_authorization" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_loop_is_terminal_on_unresolved_native_exit_or_sql(monkeypatch):
    calls=[]
    async def once(_env):
        calls.append(1)
        return m.V40ConnectedPortfolioReport(
            "V40_CONNECTED_NATIVE_CLOSE_POSITIVE_SETTLEMENT_PENDING_GET_ONLY",
            complete_exit_ports_constructed=True,
            financial_settlement_port_constructed=True,
        )
    monkeypatch.setattr(m,"observe_connected_portfolio_once",once)
    assert await m.run_connected_monitor(configuration(),max_cycles=5)==2
    assert len(calls)==1


def test_original_render_command_routes_to_concrete_worker_only_in_get_only(
    monkeypatch,
):
    calls=[]
    async def runner(env):
        calls.append(env[m.FLAG])
        return 0
    monkeypatch.setattr(m,"run_connected_monitor",runner)
    monkeypatch.setattr(sys,"argv",["worker","--run"])
    env=configuration()
    for key,value in env.items():
        monkeypatch.setenv(key,value)
    assert entry.main()==0
    assert calls==["true"]


def test_original_render_command_refuses_readonly_live_collision(monkeypatch):
    monkeypatch.setattr(sys,"argv",["worker","--run"])
    env=configuration()
    env["T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED"]="true"
    for key,value in env.items():
        monkeypatch.setenv(key,value)
    assert entry.main()==2
