"""Isolated real-money canary guardian's offline durable alert tests."""
import asyncio
import json

import pytest

import app.t100_v40_canary_durable_guardian as m
from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from tests.test_t100_v40_canary_account_guardian import ENV
from tests.test_t100_guardian_durable_incidents import (
    FakeConnection,FakeNotifier,HOOK,
)


def env(**changes):
    data={
        **ENV,
        "T100_V40_GUARDIAN_DURABLE_ALERTS_ENABLED":"true",
        "T100_V40_GUARDIAN_INCIDENT_DB_DSN":"postgresql://incidents@db.invalid/v40",
        "T100_V40_GUARDIAN_INCIDENT_DB_CA_FILE":"/ca/cert.pem",
        "T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL":HOOK,
    }
    data.update(changes)
    return data


def flat():
    return V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING",
        monitored_account_flat=True,
        inventory_two_signed_get_bookends=True,
        hedge_mode_verified=True,
        incident_alert=False,
    )


def exposed():
    return V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT",
        unexpected_positions=1,
    )


async def run_one(samples,*,db=None,notifier=None,max_cycles=None):
    db=db or FakeConnection()
    notifier=notifier or FakeNotifier(HOOK)
    samples=list(samples)
    logs=[]
    async def connect(_,__):
        return db
    async def observe(_):
        value=samples.pop(0)
        if isinstance(value,Exception):
            raise value
        return value
    async def pause(_):
        pass
    out=await m.run(
        env(),connect=connect,observe=observe,
        notifier_factory=lambda *_args,**_kwargs:notifier,
        pause=pause,emit=logs.append,
        max_cycles=max_cycles or len(samples),
    )
    return out,db,notifier,logs


def test_ready_only_means_flat_standby_no_live_or_stop_certification():
    result,db,notifier,logs=asyncio.run(run_one([flat(),flat()]))
    assert result.status=="OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
    assert result.critical_latched is False
    assert result.guardian_outbox_committed is True
    assert result.account_flat_two_get is True
    assert result.independently_verified_by_trader is False
    assert result.native_stop_lifecycle_verified is False
    assert result.live_authorization is False and result.entry_allowed is False
    assert result.mexc_posts==0 and result.trading_database_writes==0
    assert db.revision==2 and db.closed is True
    assert notifier.closed is True
    assert notifier.sent==[]
    assert len(logs)==2


def test_first_unprotected_or_unenrolled_position_latched_alert_before_discord():
    result,db,notifier,logs=asyncio.run(run_one([exposed()]))
    assert result.status=="OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER"
    assert result.critical_latched is True
    assert result.guardian_outbox_committed is True
    assert db.revision==1
    assert len(db.events)==1
    assert db.events[0]["delivered_at"]=="ACK"
    assert result.discord_delivered==1
    assert len(notifier.sent)==1
    assert "V40 DEDICATED ACCOUNT GUARDIAN"==notifier.sent[0][1]
    assert result.entry_allowed is False


def test_failed_discord_keeps_persistent_undelivered_critical_incident():
    db=FakeConnection()
    notifier=FakeNotifier(HOOK,succeed=False)
    result,db,notifier,logs=asyncio.run(run_one(
        [exposed()],db=db,notifier=notifier,
    ))
    assert result.critical_latched
    assert result.incident_pending==1
    assert db.events[0]["delivered_at"] is None
    assert db.events[0]["attempts"]==1
    assert result.discord_delivered==0


def test_network_failure_does_not_suppress_durable_critical_outbox():
    result,db,notifier,logs=asyncio.run(run_one([
        RuntimeError("SECRET_MEXC_API"),
    ]))
    assert result.status=="OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
    assert result.critical_latched
    assert len(db.events)==1
    assert "SECRET" not in logs[0]


def test_preexisting_latch_stays_blocked_on_healthy_restoration():
    db=FakeConnection()
    db.latch=True
    result,db,notifier,logs=asyncio.run(run_one([flat()],db=db))
    assert result.account_flat_two_get
    assert result.critical_latched is True
    assert result.entry_allowed is False


@pytest.mark.parametrize("changes",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_V40_GUARDIAN_DURABLE_ALERTS_ENABLED":"false"},
    {"T100_V40_GUARDIAN_INCIDENT_DB_DSN":""},
    {"T100_V40_GUARDIAN_INCIDENT_DB_CA_FILE":""},
    {"T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL":"http://discord.com"},
    {"T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL":"https://evil.net/api/webhooks/12345/token"},
    {"T100_V40_CANARY_GUARDIAN_INTERVAL_SECONDS":"1"},
    {"T100_V40_GUARDIAN_INCIDENT_DB_DSN":"postgresql://same"},
])
def test_durable_security_configuration_is_explicit_and_fail_closed(changes):
    if changes.get("T100_V40_GUARDIAN_INCIDENT_DB_DSN")=="postgresql://same":
        changes={**changes,"T100_V40_EXECUTOR_RUNTIME_DSN":"postgresql://same"}
    with pytest.raises(Exception):
        m.config(env(**changes))


def test_fixed_namespace_does_not_depend_on_secret_api_key_hash():
    assert len(m.SCOPE)==64
    assert m.SCOPE!=ENV["T100_V40_GUARDIAN_READONLY_API_KEY"]
    assert m.config(env())[3]==5



def owned_cross():
    return V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_ONE_OWNED_CROSS_SHORT_NATIVE_SL_OBSERVED_NONAUTHORIZING",
        monitored_account_flat=False,
        inventory_two_signed_get_bookends=True,
        hedge_mode_verified=True,
        incident_alert=False,
        ledger_provenance_verified=True,
        single_owned_cross_native_sl_verified=True,
    )


def test_durable_guardian_accepts_only_independently_recovered_owned_cross():
    result,db,notifier,logs=asyncio.run(run_one([
        flat(),owned_cross(),owned_cross(),
    ]))
    assert result.status=="OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
    assert result.account_flat_two_get is False
    assert result.owned_cross_native_sl_observed is True
    assert result.critical_latched is False
    assert result.incident_pending==0
    assert db.revision==3
    # The existing durable outbox also emits a WARNING on a status
    # transition (flat -> protected), even without a critical alert.
    assert all(item[3]=="warning" for item in notifier.sent)
    assert not any(item[3]=="critical" for item in notifier.sent)
    assert result.live_authorization is False and result.mexc_posts==0


def test_fake_protected_claim_without_ledger_or_native_stop_becomes_critical():
    for override in (
        dict(ledger_provenance_verified=False),
        dict(single_owned_cross_native_sl_verified=False),
        dict(hedge_mode_verified=False),
        dict(incident_alert=True),
        dict(mexc_posts=1),
    ):
        from dataclasses import replace
        forged=replace(owned_cross(),**override)
        result,db,notifier,logs=asyncio.run(run_one([forged]))
        assert result.status=="OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
        assert result.critical_latched
        assert result.incident_pending==0
        assert result.owned_cross_native_sl_observed is False
        assert len(db.events)==1


def test_live_canary_guardian_requires_distinct_checkpoint_select_role():
    options=env(
        T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED="true",
    )
    with pytest.raises(m.V40DurableGuardianHalted):
        m.config(options)
    options["T100_V40_GUARDIAN_LEDGER_READONLY_DSN"]=(
        "postgresql://guardian-readonly@fake/v40"
    )
    options["T100_V40_GUARDIAN_LEDGER_CA_FILE"]="/etc/ssl/certs/ca.pem"
    assert m.config(options)[3]==5
    for same in (
        options["T100_V40_GUARDIAN_INCIDENT_DB_DSN"],
        "postgresql://source@fake/v40",
    ):
        bad={
            **options,
            "T100_V40_GUARDIAN_LEDGER_READONLY_DSN":same,
            "T100_V40_SIGNAL_READER_DSN":"postgresql://source@fake/v40",
        }
        with pytest.raises(m.V40DurableGuardianHalted):
            m.config(bad)


def test_redacted_startup_reason_is_specific_without_leaking_secrets():
    from app.t100_v40_canary_account_guardian import V40CanaryGuardianHalted
    for error in (
        V40CanaryGuardianHalted(
            "V40_GUARDIAN_READONLY_CREDENTIALS_MISSING"
        ),
        m.V40DurableGuardianHalted(
            "V40_CROSS_OWNED_GUARDIAN_DISTINCT_READONLY_LEDGER_REQUIRED"
        ),
        m.V40DurableGuardianHalted(
            "V40_DURABLE_DATABASE_CONNECT_FAILED_SAFE_HALT"
        ),
    ):
        assert m._redacted_startup_status(error)==str(error)
    for error in (
        RuntimeError("postgresql://user:SECRET@host/db"),
        ValueError("SECRET_DISCORD_WEBHOOK_TOKEN"),
        m.V40DurableGuardianHalted(
            "V40_DURABLE_DATABASE_CONNECT_FAILED_SAFE_HALT:SECRET"
        ),
    ):
        assert m._redacted_startup_status(error)==(
            "V40_DURABLE_GUARDIAN_FAILED_SAFE_HALT"
        )


def test_connection_exception_reports_safe_database_connect_stage():
    async def connection_failure(_dsn,_ca):
        raise RuntimeError("postgresql://name:SUPER_SECRET@host/db")
    with pytest.raises(m.V40DurableGuardianHalted) as e:
        asyncio.run(m.run(
            env(),
            connect=connection_failure,
            observe=lambda _:flat(),
            max_cycles=1,
        ))
    assert str(e.value)=="V40_DURABLE_DATABASE_CONNECT_FAILED_SAFE_HALT"
    assert "SUPER_SECRET" not in str(e.value)


def test_role_failure_reports_secret_free_privilege_stage():
    class WrongRole:
        async def fetchrow(self,_sql):
            raise RuntimeError("PRIVATE_PG_PASSWORD")
        async def close(self):
            pass
    async def connect(_dsn,_ca):
        return WrongRole()
    with pytest.raises(m.V40DurableGuardianHalted) as e:
        asyncio.run(m.run(env(),connect=connect,max_cycles=1))
    assert str(e.value)=="V40_DURABLE_INCIDENT_WRITER_ROLE_FAILED_SAFE_HALT"
    assert "PRIVATE" not in str(e.value)


def test_shared_runtime_failure_identifies_missing_grants_without_disclosing_dsn(monkeypatch):
    import app.t100_v40_guardian_shared_db as shared
    cfg=env(
        T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB="true",
        T100_V40_GUARDIAN_SHARED_DB_DSN=(
            "postgresql://restricted:SECRET_PASSWORD@private.invalid/mexc_exhaustion"
        ),
        T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED="true",
    )
    async def get_connection(_dsn,_ca):
        return FakeConnection()
    async def deny_permissions(_connection):
        raise shared.V40SharedGuardianDatabaseHalted(
            "V40_SHARED_GUARDIAN_INCIDENT_GRANTS_NOT_APPLIED"
        )
    monkeypatch.setattr(
        shared,"verify_existing_runtime_for_guardian",deny_permissions
    )
    with pytest.raises(m.V40DurableGuardianHalted) as e:
        asyncio.run(m.run(cfg,connect=get_connection,max_cycles=1))
    assert str(e.value)=="V40_SHARED_GUARDIAN_INCIDENT_GRANTS_NOT_APPLIED"
    assert "SECRET_PASSWORD" not in str(e.value)
    assert m._redacted_startup_status(e.value)==str(e.value)


def test_shared_runtime_unknown_exception_remains_generic_redacted(monkeypatch):
    import app.t100_v40_guardian_shared_db as shared
    cfg=env(
        T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB="true",
        T100_V40_GUARDIAN_SHARED_DB_DSN=(
            "postgresql://restricted:SECRET_PASSWORD@private.invalid/mexc_exhaustion"
        ),
        T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED="true",
    )
    async def get_connection(_dsn,_ca):
        return FakeConnection()
    async def fail(_connection):
        raise ValueError("SECRET_PASSWORD hidden in driver message")
    monkeypatch.setattr(shared,"verify_existing_runtime_for_guardian",fail)
    with pytest.raises(m.V40DurableGuardianHalted) as e:
        asyncio.run(m.run(cfg,connect=get_connection,max_cycles=1))
    assert str(e.value)=="V40_DURABLE_INCIDENT_WRITER_ROLE_FAILED_SAFE_HALT"
    assert "SECRET_PASSWORD" not in str(e.value)


def test_guardian_status_reports_only_fixed_safe_observer_details():
    bad=V40CanaryGuardianHeartbeat(
        "https://discord.com/api/webhooks/123456789012345678/SECRET_PATH"
    )
    assert m._safe_observer_detail(bad)==(
        "V40_GUARDIAN_UNCLASSIFIED_OBSERVER_HALT"
    )
    assert m._safe_observer_detail(object())==(
        "V40_GUARDIAN_UNCLASSIFIED_OBSERVER_HALT"
    )
    assert m._safe_observer_detail(flat())==flat().status


def test_latched_status_explains_observer_stage_without_secret():
    inner=V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_INDEPENDENT_DB_OR_VENUE_GET_FAILED_HALT"
    )
    result,db,notifier,logs=asyncio.run(run_one([inner]))
    assert result.status=="OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
    assert result.observer_detail==inner.status
    assert result.critical_latched is True
    assert result.guardian_outbox_committed is True
    assert result.entry_allowed is False and result.live_authorization is False
    assert json.loads(logs[0])["observer_detail"]==inner.status


def test_diagnostic_snapshot_codes_are_bounded_not_secret_bearing():
    good=V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_SNAPSHOT_FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT_HALT"
    )
    assert m._safe_observer_detail(good)==good.status
    bad=V40CanaryGuardianHeartbeat(
        "V40_GUARDIAN_SNAPSHOT_PRIVATE_KEY_SECRET_HALT"
    )
    assert m._safe_observer_detail(bad)==(
        "V40_GUARDIAN_UNCLASSIFIED_OBSERVER_HALT"
    )
