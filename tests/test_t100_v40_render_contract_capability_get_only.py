"""V40 Render candidate contract GET-only: no order, no authorization."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.t100_v40_render_contract_capability_get_only import assess_render_capability
from scripts.t100_v40_pilot_capability_get_only import PilotCapabilityReport

KEY = "isolated-t100-pilot-futures-key"
SECRET = "isolated-t100-pilot-futures-secret"
ENV = {
    "T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED":"true",
    "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "MEXC_API_KEY":KEY,
    "MEXC_API_SECRET":SECRET,
}


def sample():
    return PilotCapabilityReport(
        status="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING",
        symbol="BTC_USDT",
        flat_hedge_account_bookended=True,
        public_futures_contract_eligible_observed=True,
        current_private_short_cross_1x_tier_observed=True,
        current_private_short_risk_limit_observed=True,
        private_short_position_leverage_observed=True,
        current_short_tier_supports_one_contract=True,
        short_margin_mode_observed="CROSS",
        short_leverage_observed=1,
    )


def call(env=ENV, symbol="BTC_USDT", report=None):
    calls=[]
    async def observe(*,key,secret,symbol):
        calls.append((key,secret,symbol))
        return sample() if report is None else report
    out=asyncio.run(assess_render_capability(env,symbol=symbol,observe=observe))
    return out,calls


def test_positive_capability_never_authorizes_real_order():
    out,calls=call()
    assert calls==[(KEY,SECRET,"BTC_USDT")]
    assert out.status=="PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING"
    assert out.flat_hedge_account_bookended
    assert out.public_futures_contract_eligible_observed
    assert out.current_private_short_cross_1x_tier_observed
    assert out.current_short_tier_supports_one_contract
    assert out.short_leverage_observed==1
    assert not out.api_key_futures_write_permission_verified
    assert not out.exact_subaccount_identity_independently_verified
    assert not out.cross_collateral_verified
    assert not out.native_market_stop_certified
    assert not out.v40_fresh_signal_verified
    assert not out.entry_allowed and not out.live_authorization
    assert out.mexc_posts==out.database_writes==0
    assert KEY not in str(out) and SECRET not in str(out)


@pytest.mark.parametrize("key,value",[
    ("T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED","false"),
    ("T100_V40_ACCOUNT_PROFILE","OTHER_ACCOUNT"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("EXECUTION_ENABLED","true"),
    ("TRADING_MODE","live"),
    ("DATABASE_URL","postgresql://old-owner:pass@db/postgres"),
    ("T100_V40_TRADER_API_KEY","ambiguous-second-key"),
    ("T100_V40_TRADER_API_SECRET","ambiguous-second-secret"),
    ("MEXC_API_KEY",""),
    ("MEXC_API_SECRET",""),
])
def test_unsafe_env_halts_before_get(key,value):
    out,calls=call({**ENV,key:value})
    assert calls==[]
    assert not out.entry_allowed
    assert not out.live_authorization
    assert out.status.endswith("_HALT")


@pytest.mark.parametrize("symbol",[
    "", "BTCUSDT","BTC_USDT;curl","ETH_USDT\nAPI_KEY",
    "BTC_USDT/../api/v1/private/order/create", "btc_usdt"
])
def test_invalid_symbol_halts_before_get(symbol):
    out,calls=call(symbol=symbol)
    assert calls==[]
    assert out.status=="V40_RENDER_CAPABILITY_SYMBOL_INVALID_HALT"


@pytest.mark.parametrize("change",[
    {"symbol":"ETH_USDT"},
    {"mexc_posts":1},
    {"db_mutations":1},
    {"live_authorization":True},
    {"entry_allowed":True},
])
def test_invalid_report_refused(change):
    out,calls=call(report=replace(sample(),**change))
    assert calls==[(KEY,SECRET,"BTC_USDT")]
    assert out.status=="V40_RENDER_CAPABILITY_REPORT_INVALID_HALT"


def test_private_get_failures_redacted():
    async def observe(*,key,secret,symbol):
        raise ValueError("MEXC_SECRET_SHOULD_NOT_ESCAPE")
    out=asyncio.run(assess_render_capability(ENV,symbol="BTC_USDT",observe=observe))
    assert out.status=="V40_RENDER_CAPABILITY_SIGNED_GET_FAILED_HALT"
    assert "MEXC_SECRET" not in str(out)


def test_bad_private_tier_is_not_promoted_to_permission():
    out,_=call(report=replace(
        sample(),status="PILOT_PRIVATE_CAPABILITY_GET_DRIFT_HALT",
        current_private_short_cross_1x_tier_observed=False,
        current_short_tier_supports_one_contract=False,
        private_capability_drift_source="RISK_LIMIT",
    ))
    assert out.status=="PILOT_PRIVATE_CAPABILITY_GET_DRIFT_HALT"
    assert out.private_capability_drift_source=="RISK_LIMIT"
    assert not out.current_private_short_cross_1x_tier_observed
    assert not out.entry_allowed and not out.live_authorization
