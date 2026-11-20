"""Offline no-network V40 Render minimal account pilot preflight regression."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

import pytest

from app.t100_v40_render_pilot_readonly_preflight import run_once
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult


KEY="read-only-pilot-key-12345"
SECRET="read-only-pilot-secret-54321"
ENV={
    "T100_V40_RENDER_PILOT_PREFLIGHT_ENABLED":"true",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "T100_V40_PILOT_ROLE":"ISOLATED_READONLY_PREFLIGHT",
    "T100_V40_PILOT_READONLY_API_KEY":KEY,
    "T100_V40_PILOT_READONLY_API_SECRET":SECRET,
    "T100_V40_PILOT_READONLY_KEY_SHA256":hashlib.sha256(KEY.encode()).hexdigest(),
}


def flat(equity="49.9",available="49.9"):
    return PilotGetOnlyResult(
        "PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
        samples=2,position_count=0,native_stop_count=0,
        account_equity_usdt=equity,available_balance_usdt=available,
        hedge_mode_observed=True,inventory_bookends_equal=True,
    )


def call(env=None,sample=None):
    observed=[]
    async def observe(*,key,secret):
        observed.append((key,secret))
        assert key==KEY and secret==SECRET
        return flat() if sample is None else sample
    report=asyncio.run(run_once(ENV if env is None else env,observe=observe))
    return report,observed


def test_real_flat_economic_cap_never_authorizes_orders_or_db_writes():
    r,observed=call()
    assert observed==[(KEY,SECRET)]
    assert r.status=="V40_RENDER_PILOT_FLAT_ECONOMICS_READ_ONLY_NONAUTHORIZING"
    assert r.pilot_notional_ceiling_usdt=="12.47500000"
    assert r.stable_flat_inventory_verified
    assert r.usdt_account_economics_observed
    assert r.account_key_fingerprint_matched
    assert not r.account_subaccount_identity_independently_verified
    assert not r.exchange_side_readonly_permission_verified
    assert not r.native_v40_short_stop_lifecycle_verified
    assert not r.autonomous_v40_adapter_certified
    assert not r.fresh_v40_signal_verified
    assert not r.entry_allowed and not r.live_authorization
    assert r.mexc_posts==r.database_writes==r.discord_requests==r.journal_writes==0
    assert KEY not in str(r) and SECRET not in str(r)
    assert KEY not in json.dumps(r.__dict__)


@pytest.mark.parametrize("name,value",[
    ("T100_V40_RENDER_PILOT_PREFLIGHT_ENABLED","false"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("EXECUTION_ENABLED","true"),
    ("TRADING_MODE","live"),
    ("T100_V40_PILOT_ROLE","T100_EXECUTOR"),
])
def test_any_paper_guard_violation_refuses_before_network(name,value):
    r,calls=call({**ENV,name:value})
    assert r.status=="V40_RENDER_PILOT_PAPER_KILL_SCOPE_HALT"
    assert calls==[] and not r.entry_allowed


def test_key_hash_mismatch_refuses_before_network():
    r,calls=call({**ENV,"T100_V40_PILOT_READONLY_KEY_SHA256":"0"*64})
    assert r.status=="V40_RENDER_PILOT_EXACT_READONLY_KEY_PIN_HALT"
    assert calls==[]


@pytest.mark.parametrize("sample",[
    replace(flat(),position_count=1),
    replace(flat(),native_stop_count=1),
    replace(flat(),samples=1),
    replace(flat(),hedge_mode_observed=False),
    replace(flat(),inventory_bookends_equal=False),
    replace(flat(),mexc_posts=1),
    replace(flat(),db_mutations=1),
    replace(flat(),live_authorization=True),
    replace(flat(),status="PILOT_UNVERIFIED"),
])
def test_any_inventory_order_or_authorization_mismatch_rejects(sample):
    r,_=call(sample=sample)
    assert r.status=="V40_RENDER_PILOT_INVENTORY_UNVERIFIED_HALT"
    assert not r.stable_flat_inventory_verified


def test_overstated_available_balance_rejects():
    r,_=call(sample=flat("49.9","50.0"))
    assert r.status=="V40_RENDER_PILOT_ECONOMICS_UNVERIFIED_HALT"


def test_zero_equity_rejects():
    r,_=call(sample=flat("0.0","0.0"))
    assert r.status=="V40_RENDER_PILOT_ECONOMICS_UNVERIFIED_HALT"


def test_failure_never_logs_private_key_or_raw_exchange_body():
    calls=[]
    async def failing(*,key,secret):
        calls.append((key,secret))
        raise RuntimeError("SECRET_KEY_OR_EXCHANGE_PAYLOAD")
    r=asyncio.run(run_once(ENV,observe=failing))
    assert calls==[(KEY,SECRET)]
    assert r.status=="V40_RENDER_PILOT_READ_ONLY_GET_FAILED_HALT"
    assert "SECRET_KEY_OR_EXCHANGE_PAYLOAD" not in str(r)
    assert SECRET not in str(r)
