"""Opt-in one existing V40 runtime PostgreSQL URL; not a trading permit."""
import asyncio

import pytest

import app.t100_v40_guardian_shared_db as m
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_db_inspect import EXPECTED_DATABASE
from tests.test_t100_v40_canary_durable_guardian import env
from app.t100_v40_canary_durable_guardian import config,V40DurableGuardianHalted


SHARED={
    "T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB":"true",
    "T100_V40_GUARDIAN_SHARED_DB_DSN":
        "postgresql://t100_live_executor_runtime@private.internal/mexc_exhaustion",
    # We no longer need the two custom CA paths or different writer/reader.
    "T100_V40_GUARDIAN_INCIDENT_DB_DSN":"",
    "T100_V40_GUARDIAN_INCIDENT_DB_CA_FILE":"",
    "T100_V40_GUARDIAN_LEDGER_READONLY_DSN":"",
    "T100_V40_GUARDIAN_LEDGER_CA_FILE":"",
    "T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED":"true",
}


def test_one_v40_runtime_dsn_replaces_two_new_db_logins_and_two_ca_paths():
    dsn,ca,hook,period=config(env(**SHARED))
    assert dsn==SHARED["T100_V40_GUARDIAN_SHARED_DB_DSN"]
    assert ca=="USE_EXISTING_V40_RUNTIME_TLS"
    assert period==5
    assert hook.startswith("https://discord.com/api/webhooks/")


@pytest.mark.parametrize("bad",[
    {"T100_V40_GUARDIAN_SHARED_DB_DSN":""},
    {"T100_V40_GUARDIAN_SHARED_DB_DSN":"http://malicious.invalid/"},
    {"T100_V40_GUARDIAN_SHARED_DB_DSN":"postgress://typo"},
    {"T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL":""},
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_V40_GUARDIAN_READONLY_API_KEY":""},
    {"T100_V40_SIGNAL_READER_DSN":SHARED[
        "T100_V40_GUARDIAN_SHARED_DB_DSN"
    ]},
])
def test_shared_db_never_bypasses_account_scope_getonly_or_real_discord(bad):
    with pytest.raises(Exception):
        config(env(**{**SHARED,**bad}))


def test_shared_flag_off_retains_strict_separate_roles():
    opts=env(**SHARED)
    opts["T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB"]="false"
    with pytest.raises(V40DurableGuardianHalted):
        config(opts)


class Conn:
    def __init__(self):
        self.current={
            "role":TARGET_ROLE,"db":EXPECTED_DATABASE,
            "read_only":"off","schema_create":False,
            "database_create":False,"admin_role":False,
        }
        self.rows=[
            {"table_name":p,"can_select":True,"can_insert":True,
             "can_update":True,"can_delete_or_truncate":False}
            for p in m._REQUIRED_TABLES
        ]
        self.calls=[]
    async def fetchrow(self,sql):
        assert "SELECT current_user AS role" in sql
        self.calls.append("GET_ROLE")
        return self.current
    async def fetch(self,sql,paths):
        assert sql.lstrip().startswith("SELECT ")
        assert paths==list(m._REQUIRED_TABLES)
        self.calls.append("GET_GRANTS")
        return self.rows


def verify(c):
    return asyncio.run(m.verify_existing_runtime_for_guardian(c))


def test_existing_runtime_user_with_incident_writes_only_is_accepted():
    c=Conn()
    verify(c)
    assert c.calls==["GET_ROLE","GET_GRANTS"]


@pytest.mark.parametrize("key,bad",[
    ("role","mexc_scanner"),
    ("role","t100_v40_signal_reader"),
    ("role","postgres"),
    ("db","unrelated_db"),
    ("read_only","on"),
    ("schema_create",True),
    ("database_create",True),
    ("admin_role",True),
])
def test_owner_admin_or_scanner_login_never_reused(key,bad):
    c=Conn()
    c.current[key]=bad
    with pytest.raises(m.V40SharedGuardianDatabaseHalted) as e:
        verify(c)
    assert "NOT_RESTRICTED_EXECUTOR_ROLE" in str(e.value)
    assert c.calls==["GET_ROLE"]


@pytest.mark.parametrize("key",[
    "can_select","can_insert","can_update",
])
def test_unapplied_incident_grants_fail_closed(key):
    c=Conn()
    c.rows[0][key]=False
    with pytest.raises(m.V40SharedGuardianDatabaseHalted,match="GRANTS_NOT_APPLIED"):
        verify(c)


def test_delete_privilege_on_incidents_denies_shared_role():
    c=Conn()
    c.rows[1]["can_delete_or_truncate"]=True
    with pytest.raises(m.V40SharedGuardianDatabaseHalted,match="GRANTS_NOT_APPLIED"):
        verify(c)


def test_raw_sql_exception_never_shows_dsn_password():
    class Fault(Conn):
        async def fetchrow(self,sql):
            raise RuntimeError("password=secret_value_123")
    with pytest.raises(m.V40SharedGuardianDatabaseHalted) as e:
        verify(Fault())
    assert "secret_value_123" not in str(e.value)
