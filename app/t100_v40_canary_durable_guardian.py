"""Independent V40 guardian, signed GET-only plus durable Discord incidents.

A dedicated Render worker for the V40 subaccount, never the old T100/225
paper shadow or BOXWAVE. It starts BEFORE the canary and observes even with
zero managed checkpoints. Any unexpected position/order, account drift,
failed MEXC GET, lost incident writer, or Discord failure is nonauthorizing.

The existing migration 035 outbox stores sticky critical events and redelivers
after restart, while a distinct minimal writer role cannot update trading
journals. This never opens/closes positions, changes a stop, or clears a
guardian latch. Position-specific native SL and trailing management remain a
separate postfill feature. A healthy standby alone cannot certify them.

Scope namespace is fixed and nonsecret; unlike the retired account enrollment
flow, no API key fingerprint is requested or stored by this worker.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict,dataclass
import hashlib
import json
import os
import re
from typing import Any,Awaitable,Callable,Mapping
from urllib.parse import urlsplit

from app.t100_live_guardian_durable_worker import (
    default_alert_connection,verify_alert_writer_role,
)
from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_live_ops_notifier import OpsNotice,T100LiveOpsNotifier
from app.t100_v40_canary_account_guardian import (
    V40CanaryGuardianHeartbeat,observe_v40_account_once,
    verify_guardian_environment,
)

SCOPE=hashlib.sha256(b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN").hexdigest()


@dataclass(frozen=True)
class V40DurableGuardianStatus:
    status: str
    critical_latched: bool=True
    incident_pending: int=0
    discord_delivered: int=0
    account_flat_two_get: bool=False
    owned_cross_native_sl_observed: bool=False
    guardian_outbox_committed: bool=False
    independently_verified_by_trader: bool=False
    native_stop_lifecycle_verified: bool=False
    full_autonomous_mode: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    trading_database_writes: int=0


class V40DurableGuardianHalted(RuntimeError):
    pass


def config(env:Mapping[str,str])->tuple[str,str,str,int]:
    verify_guardian_environment(env)
    if env.get("T100_V40_GUARDIAN_DURABLE_ALERTS_ENABLED")!="true":
        raise V40DurableGuardianHalted("V40_DURABLE_ALERTS_NOT_ENABLED")
    dsn=env.get("T100_V40_GUARDIAN_INCIDENT_DB_DSN","")
    ca=env.get("T100_V40_GUARDIAN_INCIDENT_DB_CA_FILE","")
    hook=env.get("T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL","")
    interval=env.get("T100_V40_CANARY_GUARDIAN_INTERVAL_SECONDS","5")
    if (
        type(dsn) is not str or not dsn.startswith(
            ("postgresql://","postgres://")
        )
        or type(ca) is not str or not ca.startswith("/")
        or type(hook) is not str
        or interval not in ("3","4","5")
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_ACCOUNT_OR_DB_CONFIG_INVALID")
    if dsn==env.get("T100_V40_SIGNAL_READER_DSN") or dsn==env.get(
        "T100_V40_EXECUTOR_RUNTIME_DSN"
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_WRITER_ROLE_NOT_SEPARATE")
    if env.get("T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED")=="true":
        readonly=env.get("T100_V40_GUARDIAN_LEDGER_READONLY_DSN","")
        reader_ca=env.get("T100_V40_GUARDIAN_LEDGER_CA_FILE","")
        if (
            type(readonly) is not str
            or not readonly.startswith(("postgresql://","postgres://"))
            or readonly in (
                dsn,env.get("T100_V40_SIGNAL_READER_DSN"),
                env.get("T100_V40_EXECUTOR_RUNTIME_DSN"),
            )
            or type(reader_ca) is not str
            or not reader_ca.startswith("/")
        ):
            raise V40DurableGuardianHalted(
                "V40_CROSS_OWNED_GUARDIAN_DISTINCT_READONLY_LEDGER_REQUIRED"
            )
    parsed=urlsplit(hook)
    if (
        parsed.scheme!="https"
        or parsed.hostname not in ("discord.com","discordapp.com")
        or parsed.username or parsed.password or parsed.port
        or parsed.query or parsed.fragment
        or re.fullmatch(
            r"/api/webhooks/[0-9]{10,24}/[A-Za-z0-9_-]{20,256}",
            parsed.path,
        ) is None
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_DISCORD_WEBHOOK_INVALID")
    return dsn,ca,hook,int(interval)


def _status_for_outbox(heartbeat:V40CanaryGuardianHeartbeat)->str:
    if (
        type(heartbeat) is V40CanaryGuardianHeartbeat
        and heartbeat.status==
            "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
        and heartbeat.monitored_account_flat is True
        and heartbeat.inventory_two_signed_get_bookends is True
        and heartbeat.hedge_mode_verified is True
        and heartbeat.incident_alert is False
        and heartbeat.mexc_posts==0
        and heartbeat.database_writes==0
    ):
        return "OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
    if (
        type(heartbeat) is V40CanaryGuardianHeartbeat
        and heartbeat.status==
            "V40_GUARDIAN_ONE_OWNED_CROSS_SHORT_NATIVE_SL_OBSERVED_NONAUTHORIZING"
        and heartbeat.single_owned_cross_native_sl_verified is True
        and heartbeat.ledger_provenance_verified is True
        and heartbeat.inventory_two_signed_get_bookends is True
        and heartbeat.hedge_mode_verified is True
        and heartbeat.incident_alert is False
        and heartbeat.mexc_posts==0
        and heartbeat.database_writes==0
    ):
        return "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
    if (
        type(heartbeat) is V40CanaryGuardianHeartbeat
        and heartbeat.status==
           "V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT"
    ):
        return "OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER"
    return "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"


async def run(
    env:Mapping[str,str],
    *,
    connect:Callable[[str,str],Awaitable[Any]]=default_alert_connection,
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]]|
        None=None,
    notifier_factory:Callable[...,Any]=T100LiveOpsNotifier,
    pause:Callable[[float],Awaitable[Any]]=asyncio.sleep,
    emit:Callable[[str],object]=print,
    max_cycles:int|None=None,
)->V40DurableGuardianStatus:
    dsn,ca,hook,interval=config(env)
    if observe is None:
        if env.get("T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED")=="true":
            from app.t100_v40_guardian_account_and_owned import (
                observe_v40_account_and_owned_network,
            )
            observer=observe_v40_account_and_owned_network
        else:
            observer=observe_v40_account_once
    else:
        observer=observe
    if max_cycles is not None and (
        type(max_cycles) is not int or not 1<=max_cycles<=100000
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_CYCLE_LIMIT_INVALID")
    conn=None
    notifier=None
    try:
        conn=await connect(dsn,ca)
        await verify_alert_writer_role(conn)
        outbox=GuardianIncidentOutbox(conn,scope_sha256=SCOPE)
        await outbox.acquire_singleton()
        notifier=notifier_factory(hook,cooldown_seconds=0)
        cycle=0
        while max_cycles is None or cycle<max_cycles:
            cycle+=1
            try:
                heartbeat=await asyncio.wait_for(observer(env),timeout=53)
            except Exception:
                heartbeat=V40CanaryGuardianHeartbeat(
                    "V40_GUARDIAN_SIGNED_GET_OR_RUNTIME_FAILED_HALT"
                )
            code=_status_for_outbox(heartbeat)
            healthy=code in (
                "OBSERVER_V40_CANARY_FLAT_DOUBLE_GET",
                "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL",
            )
            # Commit incident state BEFORE Discord and before continuing.
            state=await outbox.observe(status=code,alert=not healthy)
            delivered=0
            for pending in await outbox.pending(limit=10):
                await outbox.mark_attempt(pending.seq)
                try:
                    sent=await asyncio.wait_for(
                        notifier.send(OpsNotice(
                            key="v40-canary-"+pending.code,
                            title="V40 DEDICATED ACCOUNT GUARDIAN",
                            description=(
                                pending.code+
                                " — durable critical account incident. "
                                "Block entries and inspect MEXC manually."
                            ),
                            severity=pending.severity,
                        ),force=True),
                        timeout=8,
                    )
                except Exception:
                    sent=False
                if sent is not True:
                    break
                await outbox.mark_delivered(pending.seq)
                delivered+=1
            pending_count=await outbox.pending_count()
            report=V40DurableGuardianStatus(
                status=code,
                critical_latched=state.critical_latched,
                incident_pending=pending_count,
                discord_delivered=delivered,
                account_flat_two_get=(
                    code=="OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
                ),
                owned_cross_native_sl_observed=(
                    code=="OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
                ),
                guardian_outbox_committed=True,
            )
            emit(json.dumps(asdict(report),sort_keys=True),)
            if not healthy or state.critical_latched:
                return report
            if max_cycles is not None and cycle>=max_cycles:
                return report
            await pause(float(interval))
        raise V40DurableGuardianHalted("V40_DURABLE_GUARDIAN_UNEXPECTED_EXIT")
    except V40DurableGuardianHalted:
        raise
    except Exception:
        # No secrets, URLs, passwords or raw SQL in status text.
        raise V40DurableGuardianHalted("V40_DURABLE_GUARDIAN_RUNTIME_HALTED") from None
    finally:
        if conn is not None:
            await conn.close()
        if notifier is not None:
            await notifier.close()


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    if not args.run:
        print(json.dumps(asdict(V40DurableGuardianStatus(
            "V40_DURABLE_GUARDIAN_NOT_STARTED"
        )),sort_keys=True))
        return 0
    try:
        state=asyncio.run(run(
            dict(os.environ),max_cycles=1 if args.once else None,
        ))
    except Exception:
        print(json.dumps(asdict(V40DurableGuardianStatus(
            "V40_DURABLE_GUARDIAN_FAILED_SAFE_HALT"
        )),sort_keys=True))
        return 2
    return 0 if (
        (
            state.account_flat_two_get is True
            or state.owned_cross_native_sl_observed is True
        )
        and state.critical_latched is False
        and state.incident_pending==0
    ) else 2


if __name__=="__main__":
    raise SystemExit(main())
