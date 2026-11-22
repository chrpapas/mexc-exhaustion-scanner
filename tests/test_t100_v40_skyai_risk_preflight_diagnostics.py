"""Nov22 SKYAI #621: redacted risk-stage diagnostics and bounded GET retry.

All tests use fake observations. Never issue a MEXC POST, SQL mutation, or
Discord network request, and never make a bad account / contract state pass.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import t100_v40_first_canary_risk_witness as m
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness, V40CandidateMarketHalted,
)
from tests.test_t100_v40_first_canary_risk_witness import (
    quote, signal, assets, key, pnl, journal,
)


def market_with_events(events):
    market=object.__new__(MexcV40CandidateMarketWitness)
    market.private=SimpleNamespace(raw_account_assets=lambda:None)
    queue=list(events)
    calls=[]
    async def observe(_signal):
        calls.append("FULL_MARKET_BOOKEND")
        outcome=queue.pop(0)
        if isinstance(outcome,Exception):
            raise outcome
        return outcome
    market.observe=observe
    return market,calls


@pytest.mark.asyncio
@pytest.mark.parametrize("code",[
    "MARKET_SIGNED_GET_FAILED","MARKET_PUBLIC_GET_FAILED",
])
async def test_one_transient_get_only_retries_full_market_bookend(
    monkeypatch,capsys,code,
):
    market,calls=market_with_events([
        V40CandidateMarketHalted(code),quote(),
    ])
    delays=[]
    async def pause(n):delays.append(n)
    monkeypatch.setattr(m.asyncio,"sleep",pause)
    out=await m._fresh_market_observation(
        market,signal(),stage="MARKET_FIRST",
    )
    assert out.status=="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
    assert calls==["FULL_MARKET_BOOKEND"]*2
    assert delays==[.35]
    printed=capsys.readouterr().out
    assert "V40_CANARY_MARKET_READ_RETRY" in printed
    assert code in printed
    assert '"exchange_posts": 0' in printed


@pytest.mark.asyncio
async def test_second_transport_failure_halts_without_third_attempt(monkeypatch):
    code="MARKET_SIGNED_GET_FAILED"
    market,calls=market_with_events([
        V40CandidateMarketHalted(code),
        V40CandidateMarketHalted(code),quote(),
    ])
    monkeypatch.setattr(m.asyncio,"sleep",lambda _:asyncio.sleep(0))
    with pytest.raises(m.V40CanaryRiskHalted,match=(
        "V40_CANARY_RISK_MARKET_SECOND_MARKET_SIGNED_GET_FAILED"
    )):
        await m._fresh_market_observation(
            market,signal(),stage="MARKET_SECOND",
        )
    assert len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize("code",[
    "MARKET_NOT_FLAT_HEDGE_ACCOUNT",
    "MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE",
    "MARKET_ACCOUNT_BOOKEND_DRIFT",
    "MARKET_COLLATERAL_COMPONENT_DRIFT",
    "MARKET_DEPTH_NOT_STRICTLY_SORTED",
    "MARKET_PRIVATE_CONFIG_BOOKEND_DRIFT",
])
async def test_market_semantic_rejection_never_retries(monkeypatch,code):
    market,calls=market_with_events([
        V40CandidateMarketHalted(code),quote(),
    ])
    async def forbidden_sleep(_):
        raise AssertionError("UNSAFE_RETRY_OF_MARKET_OR_ACCOUNT_REJECTION")
    monkeypatch.setattr(m.asyncio,"sleep",forbidden_sleep)
    with pytest.raises(m.V40CanaryRiskHalted,match=(
        "V40_CANARY_RISK_MARKET_FIRST_"+code
    )):
        await m._fresh_market_observation(
            market,signal(),stage="MARKET_FIRST",
        )
    assert len(calls)==1


@pytest.mark.asyncio
async def test_arbitrary_exception_message_is_redacted(monkeypatch):
    market,calls=market_with_events([
        RuntimeError("postgresql://PASSWORD_PRIVATE_DB_URI"),
    ])
    reader=m.V40FirstCanaryRiskWitness(
        market=market,get_permissions=lambda:None,
        get_daily_loss=lambda:None,
        get_runtime_journal=lambda:None,
    )
    with pytest.raises(
        m.V40CanaryRiskHalted,match="^V40_CANARY_RISK_MARKET_FIRST_UNEXPECTED$"
    ) as caught:
        await reader.inspect(signal())
    assert "PASSWORD" not in str(caught.value)
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage",[
    "KEY_PERMISSIONS","DAILY_LOSS","RUNTIME_JOURNAL","SIGNED_ASSETS",
])
async def test_independent_risk_stage_can_be_identified_without_secrets(stage):
    market,calls=market_with_events([quote(),quote()])
    async def healthy():return {
        "KEY_PERMISSIONS":key(),"DAILY_LOSS":pnl(),
        "RUNTIME_JOURNAL":journal(),"SIGNED_ASSETS":assets(),
    }[current[0]]
    current=[""]
    async def get(key_name):
        current[0]=key_name
        if key_name==stage:
            raise RuntimeError("SECRET_TOKEN_MUST_NEVER_REACH_DISCORD")
        return await healthy()
    async def get_assets():return await get("SIGNED_ASSETS")
    market.private=SimpleNamespace(raw_account_assets=get_assets)
    reader=m.V40FirstCanaryRiskWitness(
        market=market,
        get_permissions=lambda:get("KEY_PERMISSIONS"),
        get_daily_loss=lambda:get("DAILY_LOSS"),
        get_runtime_journal=lambda:get("RUNTIME_JOURNAL"),
    )
    with pytest.raises(m.V40CanaryRiskHalted) as caught:
        await reader.inspect(signal())
    assert str(caught.value)=="V40_CANARY_RISK_"+stage+"_UNEXPECTED"
    assert "SECRET" not in str(caught.value)
    assert len(calls)==1
