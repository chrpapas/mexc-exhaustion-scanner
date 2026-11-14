"""V40 trader-key verification: mocked GET-only, no real venue access."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import pytest

from app.t100_v40_render_executor_key_preflight import one_get_only, safe_env
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

API_KEY = "test-trading-key-only-123"
SECRET = "test-trading-secret-only-56789"
ENV = {
    "T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED":"true",
    "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "MEXC_API_KEY":API_KEY,
    "MEXC_API_SECRET":SECRET,
}

def sample(equity="49.9", available="49.9"):
    return PilotGetOnlyResult(
        status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
        samples=2,position_count=0,native_stop_count=0,
        account_equity_usdt=equity,available_balance_usdt=available,
        hedge_mode_observed=True,inventory_bookends_equal=True
    )

def run(env=ENV, report=None):
    calls=[]
    async def observe(*,key,secret):
        calls.append((key,secret))
        return sample() if report is None else report
    out=asyncio.run(one_get_only(env,observe=observe))
    return out,calls

def test_positive_gets_are_non_authorizing_even_with_trade_key():
    out,calls=run()
    assert calls==[(API_KEY,SECRET)]
    assert out.status=="V40_TRADING_KEY_SIGNED_GET_FLAT_VERIFIED_NONAUTHORIZING"
    assert out.account_equity_usdt=="49.9"
    assert out.research_only_notional_ceiling_usdt=="12.47500000"
    assert out.signed_get_bookends_complete and out.current_flat_equity_verified
    assert not out.subaccount_identity_independently_verified
    assert not out.exchange_side_trade_permission_verified
    assert not out.cross_margin_verified and not out.native_stop_loss_verified
    assert not out.fresh_v40_signal_verified and not out.order_adapter_certified
    assert not out.independent_nonflat_monitor_certified
    assert not out.entry_allowed and not out.live_authorization
    assert out.mexc_posts==out.database_writes==out.discord_requests==0
    assert API_KEY not in str(out) and SECRET not in str(out)

@pytest.mark.parametrize("k,v",[
    ("T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED","false"),
    ("T100_V40_ACCOUNT_PROFILE","OLD_SHADOW"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("EXECUTION_ENABLED","true"),
    ("TRADING_MODE","live"),
    ("DATABASE_URL","postgresql://owner:password@localhost/trader"),
    ("T100_V40_TRADER_API_KEY","ambiguous-second-key"),
    ("T100_V40_TRADER_API_SECRET","ambiguous-second-secret"),
])
def test_rejects_broad_credentials_and_any_live_mode_before_network(k,v):
    out,calls=run({**ENV,k:v})
    assert out.status=="V40_EXECUTOR_PREFLIGHT_PAPER_OR_ISOLATION_HALT"
    assert calls==[]

def test_missing_or_bad_two_credentials_refuse_before_get():
    for k,v in [
        ("MEXC_API_KEY",""),
        ("MEXC_API_SECRET",""),
        ("MEXC_API_KEY","short"),
        ("MEXC_API_SECRET","too-short"),
        ("MEXC_API_KEY",API_KEY+" "),
    ]:
        out,calls=run({**ENV,k:v})
        assert out.status=="V40_EXECUTOR_PREFLIGHT_TRADING_CREDENTIALS_INVALID"
        assert calls==[]

@pytest.mark.parametrize("changed",[
    {"status":"PILOT_SIGNED_GET_FAILED"},
    {"samples":1},
    {"position_count":1},
    {"native_stop_count":1},
    {"hedge_mode_observed":False},
    {"inventory_bookends_equal":False},
    {"mexc_posts":1},
    {"db_mutations":1},
    {"live_authorization":True},
])
def test_any_snapshot_mismatch_halts(changed):
    out,calls=run(ENV,replace(sample(),**changed))
    assert len(calls)==1
    assert out.status=="V40_EXECUTOR_PREFLIGHT_INVENTORY_NOT_FLAT_OR_UNVERIFIED"
    assert not out.signed_get_bookends_complete

def test_bad_funds_not_approvable():
    for e,a in [("0","0"),("49","50"),("-1","-1"),("NaN","NaN")]:
        out,_=run(ENV,sample(e,a))
        assert out.status=="V40_EXECUTOR_PREFLIGHT_USDT_ECONOMICS_UNVERIFIED"
        assert not out.live_authorization

def test_raw_venue_exception_redacted():
    async def observe(*,key,secret):
        raise RuntimeError("REDACT_MY_API_SECRET")
    out=asyncio.run(one_get_only(ENV,observe=observe))
    assert out.status=="V40_EXECUTOR_PREFLIGHT_SIGNED_GET_FAILED"
    assert "REDACT_MY_API_SECRET" not in str(out)


def test_only_two_mexc_env_values_needed_no_fingerprint():
    out,calls=run(ENV)
    assert out.signed_get_bookends_complete
    assert calls==[(API_KEY,SECRET)]
    assert "T100_V40_TRADER_API_KEY_SHA256" not in ENV


def test_changing_two_credentials_only_changes_observed_account_not_authority():
    new_key="different-subaccount-key-999"
    new_secret="different-subaccount-secret-888"
    observations=[]
    async def observe(*,key,secret):
        observations.append((key,secret))
        return sample("30.0","30.0")
    new_env={**ENV,"MEXC_API_KEY":new_key,"MEXC_API_SECRET":new_secret}
    out=asyncio.run(one_get_only(new_env,observe=observe))
    assert observations==[(new_key,new_secret)]
    assert out.research_only_notional_ceiling_usdt=="7.50000000"
    assert not out.subaccount_identity_independently_verified
    assert not out.exchange_side_trade_permission_verified
    assert not out.entry_allowed and not out.live_authorization
