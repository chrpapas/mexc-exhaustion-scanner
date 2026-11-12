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
