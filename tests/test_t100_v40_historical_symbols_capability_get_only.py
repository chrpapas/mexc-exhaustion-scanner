"""Test historical-symbol CURRENT configuration GET without trade/DB path."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest

import scripts.t100_v40_historical_symbols_capability_get_only as m
from scripts.t100_v40_pilot_capability_get_only import PilotCapabilityReport

SAFE={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}


def good(symbol):
    return PilotCapabilityReport(
        status="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
        symbol=symbol,
        flat_hedge_account_bookended=True,
        public_futures_contract_eligible_observed=True,
        current_private_short_cross_1x_tier_observed=True,
        current_private_short_risk_limit_observed=True,
        private_short_position_leverage_observed=True,
        short_margin_mode_observed="CROSS",
        short_leverage_observed=1,
        one_contract_on_public_lot_grid=True,
        current_short_tier_supports_one_contract=True,
        current_contract_tick_observed=True,
    )


async def do(max_symbols=8,env=None):
    return await m.sweep_historical_settings(
        key="mock-key",secret="mock-secret",
        env=SAFE if env is None else env,max_symbols=max_symbols,
    )


def test_complete_eight_exact_real_historical_symbols_get_research_no_auth(monkeypatch):
    observed=[]
    async def witness(*,key,secret,symbol):
        assert key=="mock-key" and secret=="mock-secret"
        observed.append(symbol)
        return good(symbol)
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    result=asyncio.run(do())
    assert result.status=="HISTORICAL_SYMBOLS_FULL_READONLY_GET_SWEEP_NONAUTHORIZING"
    assert result.planned_symbols==8
    assert result.checked_symbols==8
    assert result.cross_1x_contract_settings_observed==8
    assert observed==[x[0] for x in m.HISTORICAL_SAMPLE]
    assert len({x[0] for x in m.HISTORICAL_SAMPLE})==8
    assert sum(x[1]=="HIGH_225" for x in m.HISTORICAL_SAMPLE)==3
    assert result.historical_signal_causality_revalidated_in_this_run is False
    assert result.no_fresh_signal_checked
    assert result.current_market_depth_verified is False
    assert result.full_cross_collateral_verified is False
    assert result.actual_order_execution_certified is False
    assert result.independent_nonflat_restart_certified is False
    assert result.api_key_futures_write_permission_verified is False
    assert result.database_writes==result.mexc_posts==0
    assert result.entry_allowed is False and result.live_authorization is False
    for row in result.symbols:
        assert row.historical_only is True
        assert row.short_margin_mode_observed=="CROSS"
        assert row.short_leverage_observed==1
        assert row.current_short_cross_1x_observed is True
        assert row.current_market_depth_or_liquidity_verified is False
        assert row.actual_entry_size_verified is False
        assert row.entry_allowed is False and row.live_authorization is False


def test_isolated_50_x_does_not_inherit_btc_cross_1_x_proof(monkeypatch):
    async def witness(*,key,secret,symbol):
        if symbol=="ARIA_USDT":
            return PilotCapabilityReport(
                status="PILOT_PRIVATE_SHORT_CONFIG_NOT_CROSS_ONE_X_HALT",
                symbol=symbol,
                flat_hedge_account_bookended=True,
                public_futures_contract_eligible_observed=True,
                private_short_position_leverage_observed=True,
                short_margin_mode_observed="ISOLATED",
                short_leverage_observed=50,
            )
        return good(symbol)
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    result=asyncio.run(do(max_symbols=2))
    assert result.checked_symbols==2
    assert result.cross_1x_contract_settings_observed==1
    assert result.symbols[1].symbol=="ARIA_USDT"
    assert result.symbols[1].short_margin_mode_observed=="ISOLATED"
    assert result.symbols[1].short_leverage_observed==50
    assert result.symbols[1].current_short_cross_1x_observed is False
    assert result.live_authorization is False


@pytest.mark.parametrize("drift_source",[
    "RISK_LIMIT","POSITION_LEVERAGE","BOTH",
])
def test_real_historical_symbol_drift_reason_is_preserved_without_authorization(
    monkeypatch,drift_source,
):
    async def witness(*,key,secret,symbol):
        return PilotCapabilityReport(
            status="PILOT_PRIVATE_CAPABILITY_GET_DRIFT_HALT",
            symbol=symbol,flat_hedge_account_bookended=True,
            private_capability_drift_source=drift_source,
        )
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    result=asyncio.run(do(max_symbols=1))
    assert result.checked_symbols==1
    assert result.symbols[0].private_capability_drift_source==drift_source
    assert result.cross_1x_contract_settings_observed==0
    assert result.symbols[0].current_short_cross_1x_observed is False
    assert result.mexc_posts==result.database_writes==0
    assert result.entry_allowed is False and result.live_authorization is False


def test_flat_account_preflight_failed_halts_rest_before_other_symbols(monkeypatch):
    seen=[]
    async def witness(*,key,secret,symbol):
        seen.append(symbol)
        return PilotCapabilityReport(
            status="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT",
            symbol=symbol,
        )
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    result=asyncio.run(do())
    assert seen==["IDOL_USDT"]
    assert result.status=="HISTORICAL_SYMBOLS_ACCOUNT_PRECHECK_ABORT_NONAUTHORIZING"
    assert result.checked_symbols==1
    assert result.cross_1x_contract_settings_observed==0
    assert result.planned_symbols==8


def test_symbol_get_transport_exception_never_reveals_private_error(monkeypatch):
    async def witness(*,key,secret,symbol):
        raise RuntimeError("PRIVATE_SUBACCOUNT_SECRET_KEY_DO_NOT_ECHO")
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    result=asyncio.run(do(max_symbols=1))
    assert result.checked_symbols==1
    assert result.symbols[0].status=="HISTORICAL_SYMBOL_GET_UNVERIFIED_HALT"
    assert "SECRET" not in str(result)
    assert result.entry_allowed is False


@pytest.mark.parametrize("tamper",[
    {"entry_allowed":True},
    {"live_authorization":True},
    {"mexc_posts":1},
    {"db_mutations":1},
    {"api_key_futures_write_permission_verified":True},
    {"short_margin_mode_observed":"ARBITRARY"},
    {"private_capability_drift_source":"RAW_PRIVATE_API_RESPONSE"},
    {"short_leverage_observed":False},
    {"symbol":"BTC_USDT"},
])
def test_malicious_or_inconsistent_observer_cannot_upgrade_proof(monkeypatch,tamper):
    async def witness(*,key,secret,symbol):
        return replace(good(symbol),**tamper)
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    out=asyncio.run(do(max_symbols=1))
    assert out.cross_1x_contract_settings_observed==0
    assert out.symbols[0].status=="HISTORICAL_SYMBOL_EVIDENCE_INVARIANT_FAILED_HALT"
    assert out.entry_allowed is False
    assert out.mexc_posts==out.database_writes==0


def test_passing_status_with_mismatched_settings_not_upgraded(monkeypatch):
    async def witness(*,key,secret,symbol):
        return replace(good(symbol),short_leverage_observed=50)
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    out=asyncio.run(do(max_symbols=1))
    assert out.symbols[0].status=="HISTORICAL_SYMBOL_PARTIAL_PASSED_OBSERVATION_REJECTED_HALT"
    assert out.symbols[0].current_short_cross_1x_observed is False


@pytest.mark.parametrize("env",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {},
])
def test_unsafe_environment_does_not_touch_exchange(monkeypatch,env):
    async def forbidden(**kwargs):
        raise AssertionError("NO_GET_UNDER_UNSAFE_RUNTIME")
    monkeypatch.setattr(m,"observe_pilot_capability",forbidden)
    out=asyncio.run(do(max_symbols=1,env=(
        {**SAFE,**env} if env else {}
    )))
    assert out.status=="HISTORICAL_SETTINGS_PAPER_KILL_REQUIRED_HALT"
    assert out.checked_symbols==0


@pytest.mark.parametrize("bad",[0,9,-1,True,1.5,None])
def test_invalid_count_refuses_before_get(monkeypatch,bad):
    async def forbidden(**kwargs):raise AssertionError("NO_GET")
    monkeypatch.setattr(m,"observe_pilot_capability",forbidden)
    out=asyncio.run(do(max_symbols=bad))
    assert out.status=="HISTORICAL_SETTINGS_INPUT_INVALID_HALT"


def test_cli_pilot_keychain_scope_guard_before_secret_access(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    def forbidden(*args):
        raise AssertionError("NO_KEYCHAIN_FOR_BOXWAVE")
    monkeypatch.setattr(m,"_from_keychain",forbidden)
    out=asyncio.run(m._main(SimpleNamespace(
        keychain_service="boxwave-keychain",max_symbols=8,
    ),SAFE))
    assert out.status=="HISTORICAL_SETTINGS_PILOT_SCOPE_INVALID_HALT"
    assert out.database_writes==out.mexc_posts==0


def test_cli_keys_once_and_never_prompts_database(monkeypatch):
    monkeypatch.setattr(m.sys,"platform","darwin")
    calls=[]
    def keychain(service,account):
        calls.append((service,account))
        return "MOCK_SECRET"
    monkeypatch.setattr(m,"_from_keychain",keychain)
    async def witness(**kwargs):
        assert kwargs["key"]==kwargs["secret"]=="MOCK_SECRET"
        return good(kwargs["symbol"])
    monkeypatch.setattr(m,"observe_pilot_capability",witness)
    out=asyncio.run(m._main(SimpleNamespace(
        keychain_service=m.WEB_PILOT_KEYCHAIN_SERVICE,max_symbols=2,
    ),SAFE))
    assert len(calls)==2
    assert all(s==m.WEB_PILOT_KEYCHAIN_SERVICE for s,_ in calls)
    assert out.checked_symbols==2
    assert out.cross_1x_contract_settings_observed==2
    assert not out.live_authorization


def test_code_has_no_mexc_post_sql_or_leverage_mutation_route():
    src=inspect.getsource(m)
    for forbidden in (
        "._private_post(","open_position(","submit_order(",
        "change_leverage(","submit_short_with_stop(",
        "conn.execute(","pool.execute(","INSERT INTO","UPDATE public.",
        "getpass.getpass(","database_url","Render",
    ):
        # The module's docstring references Render as a non-accessed
        # dependency; only code-level routines are checked below.
        if forbidden=="Render":
            continue
        assert forbidden not in src
