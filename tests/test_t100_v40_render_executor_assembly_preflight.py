"""Offline Render assembly attestation, with fake SELECT-only SQL contexts."""
import asyncio

import pytest

import app.t100_v40_render_executor_assembly_preflight as m
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE
from tests.test_t100_v40_executor_assembly import NoNetworkHttp
from app.t100_v40_executor_assembly import V40FirstPilotGetOnly


class RolePool:
    def __init__(self, role:str, readonly=True):
        self.role=role
        self.readonly=readonly
        self.statements=[]
        self.transactions=[]
    def acquire(self):
        self.statements.append("ACQUIRE")
        return RoleConn(self)


class RoleConn:
    def __init__(self,pool):
        self.pool=pool
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    def transaction(self,*,isolation,readonly):
        assert isolation=="repeatable_read"
        assert readonly is True
        self.pool.transactions.append((isolation,readonly))
        return self
    async def fetchval(self,sql):
        self.pool.statements.append(sql)
        responses={
            "SELECT current_user":self.pool.role,
            "SELECT current_database()":"mexc_exhaustion",
            "SELECT current_setting('transaction_isolation')":"repeatable read",
            "SELECT current_setting('transaction_read_only')":(
                "on" if self.pool.readonly else "off"
            ),
        }
        assert sql in responses
        return responses[sql]


def sources():
    source=RolePool(RESTRICTED_V40_SIGNAL_READER_ROLE)
    runtime=RolePool(TARGET_ROLE)
    http=NoNetworkHttp()
    signed=V40FirstPilotGetOnly(
        http,api_key="test-account-key-12345678",
        api_secret="test-account-secret-12345678",
    )
    return source,runtime,signed,http


def test_real_roles_and_disabled_executor_assembly_attested_with_selects_only():
    src,run,get,http=sources()
    x=asyncio.run(m.inspect_v40_assembly(
        signal_pool=src,runtime_pool=run,
        signed_get=get,public_get=http,
    ))
    assert x.status=="V40_EXECUTOR_ASSEMBLED_TWO_ROLES_GET_ONLY_NO_LIVE_POST"
    assert x.separate_database_roles_verified
    assert x.source_role_readonly_verified
    assert x.executor_role_readonly_verified
    assert x.coordinator_wired and x.strategy_source_wired
    assert x.exact_market_depth_wired and x.dispatch_claim_wired
    assert x.protected_commit_wired and x.initial_pilot_flat_only
    assert not x.native_sl75_execution_certified
    assert not x.independently_running_guardian_certified
    assert not x.cross_margin_risk_certified
    assert not x.live_post_adapter_connected
    assert x.exchange_posts==0 and x.database_writes==0
    assert not x.entry_allowed and not x.live_authorization
    assert http.calls==[]
    for pool in (src,run):
        assert pool.transactions==[("repeatable_read",True)]
        assert all(
            statement=="ACQUIRE" or statement.startswith("SELECT ")
            for statement in pool.statements
        )


@pytest.mark.parametrize("broken",[
    {"source_role":"postgres"},
    {"runtime_role":"postgres"},
    {"source_readonly":False},
    {"runtime_readonly":False},
])
def test_role_or_read_only_drift_halts_before_any_exchange_request(broken):
    src,run,get,http=sources()
    src.role=broken.get("source_role",src.role)
    run.role=broken.get("runtime_role",run.role)
    src.readonly=broken.get("source_readonly",src.readonly)
    run.readonly=broken.get("runtime_readonly",run.readonly)
    report=asyncio.run(m.inspect_v40_assembly(
        signal_pool=src,runtime_pool=run,
        signed_get=get,public_get=http,
    ))
    assert report.status=="V40_EXECUTOR_ASSEMBLY_ROLE_READONLY_CHECK_HALT"
    assert not report.coordinator_wired
    assert report.exchange_posts==0 and report.database_writes==0
    assert http.calls==[]


def test_same_pool_cannot_spoof_role_separation():
    src,run,get,http=sources()
    report=asyncio.run(m.inspect_v40_assembly(
        signal_pool=src,runtime_pool=src,signed_get=get,public_get=http,
    ))
    assert report.status=="V40_EXECUTOR_ASSEMBLY_ROLES_NOT_SEPARATE_HALT"
    assert src.transactions==[] and http.calls==[]


def test_readonly_render_flag_is_default_off_and_all_live_switches_block():
    safe={
        "T100_V40_EXECUTOR_ASSEMBLY_GET_ONLY_ENABLED":"true",
        "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
        "T100_LIVE_KILL_SWITCH":"true",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_EXECUTION_MODE":"paper",
        "EXECUTION_ENABLED":"false","TRADING_MODE":"paper",
        "T100_V40_REAL_SIGNAL_MONITOR_ENABLED":"true",
        "T100_V40_MONITOR_SINGLE_POSITION_SCOPE":"true",
        "MEXC_API_KEY":"test-account-key-12345678",
        "MEXC_API_SECRET":"test-account-secret-12345678",
        "T100_V40_SIGNAL_READER_DSN":"postgresql://fake-invalid",
        "T100_V40_EXECUTOR_RUNTIME_DSN":"postgresql://fake-invalid",
    }
    # Do not rely on monkeypatching external hosts for the default-off
    # contract. This code returns before imports or opening the DSNs.
    for broken in (
        {"T100_V40_EXECUTOR_ASSEMBLY_GET_ONLY_ENABLED":"false"},
        {"T100_V40_EXECUTOR_DISABLED_WORKER":"false"},
        {"T100_LIVE_KILL_SWITCH":"false"},
        {"T100_LIVE_ORDER_API_ENABLED":"true"},
        {"T100_EXECUTION_MODE":"live"},
        {"EXECUTION_ENABLED":"true"},
        {"TRADING_MODE":"live"},
    ):
        env={**safe,**broken}
        result=asyncio.run(m.observe_v40_assembly_render(env))
        assert result.status=="V40_EXECUTOR_ASSEMBLY_PAPER_KILL_OR_SCOPE_HALT"
        assert not result.coordinator_wired
        assert result.exchange_posts==0 and result.database_writes==0
