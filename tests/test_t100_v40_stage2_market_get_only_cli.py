"""V40 Stage2 -> live pilot market research: all deterministic, no network."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from types import SimpleNamespace
import sys

import pytest

import scripts.t100_v40_stage2_market_get_only_cli as cli
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source,V40SourceBatch,
)
from tests.test_t100_live_v40_candidate_market_witness import (
    Private,Public,Clock,START,END,
)
from tests.test_t100_live_v40_entry_coordinator import candidate

PAPER={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}
NOW=datetime(2026,11,10,20,45,tzinfo=timezone.utc)


def source(signals=(),status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"):
    obj=PostgresV40Stage2Source(pool=object())
    calls=[]
    async def latest_candidates(*,observed_at,limit):
        calls.append((observed_at,limit))
        return V40SourceBatch(
            status=status,observed_at=observed_at,
            candidates=tuple(signals),
        )
    obj.latest_candidates=latest_candidates
    return obj,calls


def check(*,signals=None,private=None,public=None,env=None,max_checks=4):
    src,calls=source(
        signals=[candidate()] if signals is None else signals,
    )
    priv=private or Private()
    pub=public or Public()
    w=ReadOnlyMexcProbe(priv)
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=w,public=pub,
        clock_ms=Clock(*([START,END]*4)),
        observed_at=NOW,max_checks=max_checks,
        env=PAPER if env is None else env,
    ))
    return out,calls,priv,pub


def test_fresh_stage2_symbol_auto_discovered_and_quoted_with_no_post():
    out,calls,priv,pub=check()
    assert out.status=="STAGE2_MARKET_BOUNDED_GET_SWEEP_NONAUTHORIZING"
    assert out.fresh_validated_signals==1
    assert out.checked_symbols==1
    assert not out.limited_for_research
    assert len(out.candidates)==1
    item=out.candidates[0]
    assert item.symbol=="EXAMPLE_USDT"
    assert item.tier in ("HIGH_225","LOW_100")
    assert item.status=="STAGE2_MARKET_CANDIDATE_GET_QUOTED_NONAUTHORIZING"
    assert item.short_cross_1x_bookended
    assert item.public_contract_eligible
    assert item.fresh_depth_observed
    assert item.private_order_write_permission_verified is False
    assert out.actual_order_execution_certified is False
    assert out.independent_risk_and_collateral_certified is False
    assert out.independent_autonomous_restart_certified is False
    assert out.live_authorization is False
    assert out.entry_allowed is False
    assert out.database_writes==out.mexc_posts==0
    assert out.read_only
    assert calls==[(NOW,32)]
    assert len(priv.calls)==14
    assert len(pub.calls)==2


def test_preview_reports_only_normalized_risk_row_order_not_auth(monkeypatch):
    from app.t100_live_v40_candidate_market_witness import (
        MexcV40CandidateMarketWitness,V40CandidateQuote,
    )
    original=MexcV40CandidateMarketWitness.observe
    async def reordered_observation(self, signal):
        report=await original(self,signal)
        return replace(report,private_risk_row_order_normalized=True)
    monkeypatch.setattr(
        MexcV40CandidateMarketWitness,"observe",reordered_observation,
    )
    out,_,private,public=check()
    assert out.candidates[0].private_risk_row_order_normalized is True
    assert out.candidates[0].private_order_write_permission_verified is False
    assert out.live_authorization is False
    assert out.entry_allowed is False
    assert out.database_writes==out.mexc_posts==0


def test_no_fresh_stage2_signals_does_not_invent_symbols_or_query_exchange():
    out,calls,priv,pub=check(signals=[])
    assert out.status=="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING"
    assert out.fresh_validated_signals==0
    assert out.candidates==()
    assert priv.calls==[]
    assert pub.calls==[]


def test_current_real_short_isolated_50_x_does_not_pass_stage2_market():
    private=Private()
    private.private_leverage[1].update(openType=1,leverage=50)
    out,_,priv,_=check(private=private)
    assert out.status=="STAGE2_MARKET_BOUNDED_GET_SWEEP_NONAUTHORIZING"
    assert out.candidates[0].status=="MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED"
    assert out.candidates[0].short_cross_1x_bookended is False
    assert out.live_authorization is False
    assert out.mexc_posts==0


def test_missing_private_leverage_response_does_not_fallback_to_risk_limit():
    private=Private()
    private.private_leverage=[]
    out,_,_,_=check(private=private)
    assert out.candidates[0].status=="MARKET_SIGNED_GET_FAILED"
    assert not out.candidates[0].short_cross_1x_bookended
    assert not out.entry_allowed


def test_bounded_newest_first_cannot_claim_to_audit_entire_stage2_batch():
    c=candidate()
    signals=[
        replace(c,signal_id=10,admission_open_ts=c.admission_open_ts-100),
        replace(c,signal_id=11,admission_open_ts=c.admission_open_ts-60),
        replace(c,signal_id=12,admission_open_ts=c.admission_open_ts),
    ]
    out,calls,priv,pub=check(signals=signals,max_checks=2)
    assert out.fresh_validated_signals==3
    assert out.checked_symbols==2
    assert out.limited_for_research
    assert len(out.candidates)==2
    assert out.read_only
    assert out.mexc_posts==0
    assert len(priv.calls)==28
    assert len(pub.calls)==4


def test_duplicate_stage2_source_ids_freeze_before_market_get():
    c=candidate()
    signals=[c,replace(c,tier=c.tier)]
    out,_,priv,pub=check(signals=signals)
    assert out.status=="STAGE2_MARKET_DUPLICATE_SIGNAL_SOURCE_HALT"
    assert priv.calls==pub.calls==[]


@pytest.mark.parametrize("symbol",["BTC_USDT","ETH_USDT","../GET","abcd_usdt"])
def test_unsupported_or_injected_signal_symbol_halts_before_venue(symbol):
    out,_,priv,pub=check(signals=[replace(candidate(),symbol=symbol)])
    assert out.status=="STAGE2_MARKET_SIGNAL_IDENTITY_UNVERIFIED_HALT"
    assert priv.calls==[]
    assert pub.calls==[]
    assert not out.live_authorization


def test_unsafe_runtime_refuses_before_db_or_exchange():
    src,calls=source([candidate()])
    priv=Private();pub=Public()
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=ReadOnlyMexcProbe(priv),public=pub,
        clock_ms=Clock(START,END),observed_at=NOW,env={
            **PAPER,"T100_LIVE_KILL_SWITCH":"false",
        },
    ))
    assert out.status=="STAGE2_MARKET_PAPER_KILL_REQUIRED_HALT"
    assert calls==[]
    assert priv.calls==pub.calls==[]


def test_unknown_source_status_never_claims_success():
    src,calls=source([candidate()],status="STAGE2_UNTRUSTED")
    priv=Private();pub=Public()
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=ReadOnlyMexcProbe(priv),public=pub,
        clock_ms=Clock(START,END),observed_at=NOW,env=PAPER,
    ))
    assert out.status=="STAGE2_MARKET_SOURCE_CONTRACT_INVALID_HALT"
    assert priv.calls==pub.calls==[]
    assert not out.entry_allowed


def test_read_only_source_throws_secret_exception_without_leaking():
    src,calls=source([candidate()])
    async def rejected(**kwargs):
        raise RuntimeError("RENDER_PRIVATE_DB_PASSWORD")
    src.latest_candidates=rejected
    priv=Private();pub=Public()
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=ReadOnlyMexcProbe(priv),public=pub,
        clock_ms=Clock(START,END),observed_at=NOW,env=PAPER,
    ))
    assert out.status=="STAGE2_MARKET_READONLY_SOURCE_UNVERIFIED_HALT"
    assert "PASSWORD" not in str(out)
    assert priv.calls==pub.calls==[]


def test_scope_or_invalid_runtime_rejected_before_keychain_and_password(monkeypatch):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    def forbidden(*args,**kwargs):
        raise AssertionError("MUST_NOT_REACH_KEYCHAIN_OR_DB")
    monkeypatch.setattr(cli,"_from_keychain",forbidden)
    args=SimpleNamespace(
        keychain_service="boxwave-mexc",max_checks=4,
    )
    out=asyncio.run(cli._main(args,PAPER))
    assert out.status=="STAGE2_MARKET_PILOT_SCOPE_INVALID_HALT"
    args.keychain_service=cli.WEB_PILOT_KEYCHAIN_SERVICE
    out=asyncio.run(cli._main(args,{
        **PAPER,"T100_LIVE_ORDER_API_ENABLED":"true",
    }))
    assert out.status=="STAGE2_MARKET_PAPER_KILL_REQUIRED_HALT"


def test_restricted_sql_and_keychain_code_has_no_order_submission():
    import inspect
    text=inspect.getsource(cli)
    for forbidden in (
        "._private_post(", "submit_short_with_stop(",
        "change_leverage(", "place_position_stop(", "conn.execute(",
        "trigger_deploy(", "pool.execute(", "INSERT INTO", "UPDATE public.",
    ):
        assert forbidden not in text


def test_fixed_source_failure_is_exposed_without_secret_contents():
    from app.t100_live_v40_stage2_source import V40SignalIntakeHalted
    src,_=source([candidate()])
    async def missing_role(**kwargs):
        raise V40SignalIntakeHalted(
            "SOURCE_RESTRICTED_REPEATABLE_READ_CONTEXT_REQUIRED"
        )
    src.latest_candidates=missing_role
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=ReadOnlyMexcProbe(Private()),public=Public(),
        clock_ms=Clock(START,END),observed_at=NOW,env=PAPER,
    ))
    assert out.status==(
        "STAGE2_MARKET_SOURCE_RESTRICTED_REPEATABLE_READ_CONTEXT_REQUIRED_HALT"
    )
    assert not out.entry_allowed


def test_unrecognized_source_error_cannot_be_echoed_as_password():
    from app.t100_live_v40_stage2_source import V40SignalIntakeHalted
    src,_=source([candidate()])
    async def secret_error(**kwargs):
        raise V40SignalIntakeHalted("SOURCE_PASSWORD_IS_VERY_SECRET")
    src.latest_candidates=secret_error
    out=asyncio.run(cli.preview_fresh_stage2(
        source=src,private=ReadOnlyMexcProbe(Private()),public=Public(),
        clock_ms=Clock(START,END),observed_at=NOW,env=PAPER,
    ))
    assert out.status=="STAGE2_MARKET_READONLY_SOURCE_UNVERIFIED_HALT"
    assert "PASSWORD" not in str(out)


def test_mac_owner_url_has_one_hidden_prompt_and_no_executor_password(
    monkeypatch,
):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    entries=[]
    monkeypatch.setattr(
        cli,"_from_keychain",
        lambda service,account:(
            entries.append(("KEYCHAIN",service,account)) or "FAKE_KEY"
        ),
    )
    prompts=[]
    url="postgresql://mexc_scanner:fake_pass@localhost/mexc_exhaustion"
    def one_hidden_prompt(question):
        prompts.append(question)
        return url
    monkeypatch.setattr(cli.getpass,"getpass",one_hidden_prompt)
    pools=[]
    class FakePool:
        async def close(self):pools.append("CLOSED")
    async def pool_factory(**kw):
        pools.append(kw)
        return FakePool()
    monkeypatch.setitem(
        sys.modules,"asyncpg",
        SimpleNamespace(create_pool=pool_factory),
    )
    class Signed:
        def __init__(self,*,key,secret):
            assert key==secret=="FAKE_KEY"
        async def close(self):entries.append("SIGNED_CLOSED")
    monkeypatch.setattr(cli,"KeychainSignedGetOnly",Signed)
    class PublicCtx:
        async def __aenter__(self):return object()
        async def __aexit__(self,*_):return None
    monkeypatch.setattr(cli.httpx,"AsyncClient",lambda **kw:PublicCtx())
    async def fake_preview(**kw):
        assert kw["source"].manual_scanner_owner_readonly is True
        assert kw["source"].expected_source_role=="mexc_scanner"
        assert kw["env"]==PAPER
        return cli.Stage2Preview(
            status="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING",
        )
    monkeypatch.setattr(cli,"preview_fresh_stage2",fake_preview)
    out=asyncio.run(cli._main(SimpleNamespace(
        keychain_service=cli.WEB_PILOT_KEYCHAIN_SERVICE,max_checks=4,
    ),PAPER))
    assert out.status=="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING"
    assert len(prompts)==1
    assert "External URL" in prompts[0]
    assert "password" not in prompts[0].lower()
    assert isinstance(pools[0],dict)
    assert pools[0]["dsn"]==url
    assert "user" not in pools[0]
    assert "password" not in pools[0]
    assert pools[0]["server_settings"]=={
        "default_transaction_read_only":"on",
    }
    assert pools[1]=="CLOSED"
    assert "SIGNED_CLOSED" in entries
    assert out.mexc_posts==out.database_writes==0


def test_mac_owner_url_missing_refuses_without_connecting_to_db(monkeypatch):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    monkeypatch.setattr(cli,"_from_keychain",lambda *_:"SAFE_MOCK_KEY")
    monkeypatch.setattr(cli.getpass,"getpass",lambda _:"")
    out=asyncio.run(cli._main(SimpleNamespace(
        keychain_service=cli.WEB_PILOT_KEYCHAIN_SERVICE,max_checks=4,
    ),PAPER))
    assert out.status=="STAGE2_MARKET_SCANNER_OWNER_URL_REQUIRED_HALT"
    assert out.database_writes==out.mexc_posts==0


def test_empty_live_batch_retains_no_signal_status_and_includes_separate_health(monkeypatch):
    from app.t100_live_v40_stage2_source_health import V40Stage2SourceHealth
    calls=[]
    async def health(source,*,observed_at):
        calls.append(observed_at)
        return V40Stage2SourceHealth(
            status="STAGE2_SCANNER_RECENT_HEARTBEAT_AND_EVALUATION_OBSERVED_NONAUTHORIZING",
            eligible_stage2_events_last_7d=3,
            scanner_heartbeat_recent=True,
            scanner_evaluation_recent=True,
            database_and_scanner_role_verified=True,
        )
    monkeypatch.setattr(cli,"inspect_scanner_source_health",health)
    out,source_calls,private,public=check(signals=[])
    assert out.status=="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING"
    assert out.fresh_validated_signals==0
    assert out.checked_symbols==0
    assert out.source_health is not None
    assert out.source_health.eligible_stage2_events_last_7d==3
    assert out.source_health.scanner_heartbeat_recent is True
    assert out.entry_allowed is False
    assert not out.live_authorization
    assert private.calls==public.calls==[]
    assert calls==[NOW]


def test_failed_optional_health_does_not_reclassify_valid_empty_live_intake(monkeypatch):
    async def failed(*args,**kwargs):
        raise ValueError("DB_SECRET_RAISED_FROM_MOCK")
    monkeypatch.setattr(cli,"inspect_scanner_source_health",failed)
    out,_,private,public=check(signals=[])
    assert out.status=="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING"
    assert out.source_health.status=="STAGE2_SOURCE_HEALTH_READONLY_INSPECTION_FAILED_HALT"
    assert "SECRET" not in str(out)
    assert private.calls==public.calls==[]
    assert out.database_writes==out.mexc_posts==0


def test_nonempty_source_does_not_run_independent_historical_health_query(monkeypatch):
    async def forbidden(*args,**kwargs):
        raise AssertionError("HEALTH_ONLY_AFTER_EMPTY_LIVE_INTAKE")
    monkeypatch.setattr(cli,"inspect_scanner_source_health",forbidden)
    out,_,_,_=check()
    assert out.status=="STAGE2_MARKET_BOUNDED_GET_SWEEP_NONAUTHORIZING"
    assert out.source_health is None
    assert not out.entry_allowed
