"""Historical Stage2 Mac CLI cannot reach MEXC or privileged SQL writes."""
from __future__ import annotations

import asyncio
import inspect
import sys
from types import SimpleNamespace

import pytest

import scripts.t100_v40_historical_stage2_audit_cli as cli

PAPER={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}

URL="postgresql://mexc_scanner:fake_pass@localhost/mexc_exhaustion"


@pytest.mark.parametrize("invalid",[
    "","example","postgresql://t100_live_executor_runtime:xx@localhost/mexc_exhaustion",
    "postgresql://mexc_scanner:xx@localhost/wrong_db",
    "postgresql://mexc_scanner@localhost/mexc_exhaustion",
    "https://mexc_scanner:xx@localhost/mexc_exhaustion",
    "postgresql://mexc_scanner:xx@/mexc_exhaustion",
])
def test_non_scanner_owner_url_scope_refused_without_connecting(invalid):
    assert not cli._url_identity_only(invalid)


def test_single_scanner_owner_url_is_validated_locally():
    assert cli._url_identity_only(URL)
    assert cli._url_identity_only(URL.replace("postgresql:","postgres:"))


@pytest.mark.parametrize("field,value",[
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
])
def test_live_or_kill_disabled_refuses_before_password_or_database(
    monkeypatch,field,value,
):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    def fail_prompt(*args):
        raise AssertionError("MUST_NOT_PROMPT_UNSAFE_MODE")
    monkeypatch.setattr(cli.getpass,"getpass",fail_prompt)
    out=asyncio.run(cli._main(
        SimpleNamespace(max_records=8,lookback_days=7),
        {**PAPER,field:value},
    ))
    assert out.status=="V40_HISTORICAL_PAPER_KILL_REQUIRED_HALT"


@pytest.mark.parametrize("max_records,lookback_days",[(9,7),(8,181),(0,7),(True,7)])
def test_unbounded_research_refuses_without_db_connection(
    monkeypatch,max_records,lookback_days,
):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    monkeypatch.setattr(
        cli.getpass,"getpass",
        lambda *_:pytest.fail("NO_URL_REQUEST_FOR_INVALID_LIMIT"),
    )
    out=asyncio.run(cli._main(
        SimpleNamespace(max_records=max_records,lookback_days=lookback_days),
        PAPER,
    ))
    assert out.status=="V40_HISTORICAL_LIMITS_INVALID_HALT"


def test_manual_historical_runner_uses_one_hidden_url_and_readonly_pool(
    monkeypatch,
):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    prompts=[]
    def hidden(question):
        prompts.append(question)
        return URL
    monkeypatch.setattr(cli.getpass,"getpass",hidden)
    captured=[]
    class Pool:
        async def close(self):captured.append("CLOSED")
    async def make_pool(**kwargs):
        captured.append(kwargs)
        return Pool()
    monkeypatch.setitem(sys.modules,"asyncpg",SimpleNamespace(
        create_pool=make_pool,
    ))
    async def replay(source,*,observed_at,max_records,lookback_days):
        assert source.manual_scanner_owner_readonly is True
        assert source.expected_source_role=="mexc_scanner"
        assert observed_at.tzinfo is not None
        assert max_records==8 and lookback_days==7
        return cli.HistoricalStage2Audit(
            status="V40_HISTORICAL_STAGE2_SAMPLE_EMPTY_NONAUTHORIZING",
        )
    monkeypatch.setattr(cli,"inspect_historical_stage2",replay)
    result=asyncio.run(cli._main(
        SimpleNamespace(max_records=8,lookback_days=7),PAPER,
    ))
    assert result.status=="V40_HISTORICAL_STAGE2_SAMPLE_EMPTY_NONAUTHORIZING"
    assert len(prompts)==1
    assert "External URL" in prompts[0]
    assert "password" not in prompts[0].lower()
    opts=captured[0]
    assert opts["dsn"]==URL
    assert "password" not in opts and "user" not in opts
    assert opts["min_size"]==1 and opts["max_size"]==1
    assert opts["server_settings"]=={"default_transaction_read_only":"on"}
    assert captured[-1]=="CLOSED"
    assert result.entry_allowed is False
    assert result.live_authorization is False
    assert result.mexc_posts==result.database_writes==0


def test_db_connection_secret_is_redacted_and_denies_trade(monkeypatch):
    monkeypatch.setattr(cli.sys,"platform","darwin")
    monkeypatch.setattr(cli.getpass,"getpass",lambda _:URL)
    async def broken(**kwargs):
        raise ConnectionError("PRIVATE_POSTGRESQL_PASSWORD_IN_EXCEPTION")
    monkeypatch.setitem(
        sys.modules,"asyncpg",SimpleNamespace(create_pool=broken),
    )
    report=asyncio.run(cli._main(
        SimpleNamespace(max_records=8,lookback_days=7),PAPER,
    ))
    assert report.status=="V40_HISTORICAL_DB_CONNECTION_FAILED_HALT"
    assert "PASSWORD" not in str(report)
    assert report.entry_allowed is False
    assert report.mexc_posts==report.database_writes==0


def test_code_has_no_mexc_keychain_order_or_database_mutation_path():
    source=inspect.getsource(cli)
    for banned in (
        "._private_get(","._private_post(","security find-generic-password",
        "mexc.trade","place_order(","submit_short_with_stop(",
        "INSERT INTO","UPDATE public.","conn.execute(","pool.execute(",
        "set_leverage(","trigger_deploy(",
    ):
        assert banned not in source
