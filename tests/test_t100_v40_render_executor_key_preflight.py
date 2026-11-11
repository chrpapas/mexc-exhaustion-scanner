"""V40 trader-key verification: mocked GET-only, no real venue access."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import hashlib
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
    "T100_V40_TRADER_API_KEY":API_KEY,
    "T100_V40_TRADER_API_SECRET":SECRET,
    "T100_V40_TRADER_API_KEY_SHA256":hashlib.sha256(API_KEY.encode()).hexdigest(),
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
    ("MEXC_API_KEY","legacy-direct-key"),
    ("MEXC_API_SECRET","legacy-direct-secret"),
])
def test_rejects_broad_credentials_and_any_live_mode_before_network(k,v):
    out,calls=run({**ENV,k:v})
    assert out.status=="V40_EXECUTOR_PREFLIGHT_PAPER_OR_ISOLATION_HALT"
    assert calls==[]

def test_missing_key_secret_or_pin_refuses_before_get():
    for k,v in [
        ("T100_V40_TRADER_API_KEY",""),
        ("T100_V40_TRADER_API_SECRET",""),
        ("T100_V40_TRADER_API_KEY_SHA256","0"*64),
    ]:
        out,calls=run({**ENV,k:v})
        assert out.status=="V40_EXECUTOR_PREFLIGHT_EXACT_TRADING_KEY_PIN_HALT"
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
