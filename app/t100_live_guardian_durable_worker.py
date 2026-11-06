"""Dedicated durable T100 guardian incident dispatcher (review-only).

The managed-position observer remains signed MEXC GET + SELECT-only PG.
A DIFFERENT isolated PG role stores guardian health/alerts in migration 035;
only a Discord webhook is posted (NEVER an MEXC order). Unacknowledged
incidents survive restarts and are redelivered at least once.

This service has no call to exchange POST, stop edits, T100 journals,
manual-position management, account enrollment or release authorization.
Not deployed; explicit flags, database grants and private webhook required.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
import re
import ssl
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from app.t100_live_guardian_readonly_once import (
    ReadOnlyCycleSummary,run_with_network,validate_once_config,
)
from app.t100_live_guardian_readonly_worker import (
    GuardianWorkerConfig,GuardianHeartbeat,_classify,
)
from app.t100_live_guardian_incident_outbox import (
    GuardianIncidentHalted,GuardianIncidentOutbox,
)
from app.t100_live_ops_notifier import OpsNotice,T100LiveOpsNotifier


class GuardianDurableHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class GuardianDurableCycle:
    status: str
    alert: bool
    critical_latched: bool
    pending: int
    delivered_this_cycle: int
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: int = 0
    managed_checkpoint_mutations: int = 0


def durable_config(env: Mapping[str,str]) -> tuple[GuardianWorkerConfig,str,str,str]:
    if env.get("T100_GUARDIAN_DURABLE_ALERTS_ENABLED") != "true":
        raise GuardianDurableHalted("GUARDIAN_DURABLE_INCIDENTS_NOT_ENABLED")
    config=GuardianWorkerConfig.from_env(env)
    validated=validate_once_config(env)
    dsn=env.get("T100_GUARDIAN_INCIDENT_DB_DSN","")
    hook=env.get("T100_GUARDIAN_DISCORD_WEBHOOK_URL","")
    if (type(dsn) is not str or
        not dsn.startswith(("postgresql://","postgres://")) or
        dsn==validated["dsn"]):
        raise GuardianDurableHalted("GUARDIAN_DISTINCT_ALERT_DB_ROLE_REQUIRED")
    if type(hook) is not str:
        raise GuardianDurableHalted("GUARDIAN_DISCORD_HOOK_INVALID")
    parsed=urlsplit(hook)
    if (parsed.scheme!="https" or
            parsed.hostname not in ("discord.com","discordapp.com") or
            parsed.username is not None or parsed.password is not None or
            parsed.port is not None or parsed.query or parsed.fragment or
            re.fullmatch(r"/api/webhooks/[0-9]{10,24}/[A-Za-z0-9_-]{20,256}",
                         parsed.path) is None):
        raise GuardianDurableHalted("GUARDIAN_DISCORD_HOOK_NOT_ALLOWLISTED")
    return config,dsn,hook,validated["ca_file"]


async def default_alert_connection(dsn: str, ca_file: str) -> Any:
    import asyncpg
    context=ssl.create_default_context(cafile=ca_file)
    context.check_hostname=True
    context.verify_mode=ssl.CERT_REQUIRED
    return await asyncpg.connect(dsn,ssl=context,timeout=8,command_timeout=15)


async def verify_alert_writer_role(conn: Any) -> None:
    """Must have ONLY guardian-incident write grants, not trade grants."""
    try:
        row=await conn.fetchrow(
            """SELECT current_setting('transaction_read_only') AS read_only,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_state','INSERT') AS incident_insert,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_state','UPDATE') AS incident_update,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_state','DELETE') AS incident_delete,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_state','TRUNCATE') AS incident_truncate,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_outbox','SELECT') AS outbox_select,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_outbox','UPDATE') AS outbox_update,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_outbox','DELETE') AS outbox_delete,
                has_table_privilege(current_user,
                    'public.t100_guardian_incident_outbox','TRUNCATE') AS outbox_truncate,
                has_table_privilege(current_user,
                    'public.t100_live_position_checkpoints','INSERT') AS checkpoint_insert,
                has_table_privilege(current_user,
                    'public.t100_live_position_checkpoints','UPDATE') AS checkpoint_update,
                has_table_privilege(current_user,
                    'public.t100_live_position_checkpoints','DELETE') AS checkpoint_delete,
                has_table_privilege(current_user,
                    'public.t100_live_order_intents','INSERT') AS trade_intent_insert,
                has_table_privilege(current_user,
                    'public.t100_live_stop_amendments','UPDATE') AS stop_update,
                has_table_privilege(current_user,
                    'public.t100_owned_emergency_close_intents','UPDATE') AS emergency_update,
                has_table_privilege(current_user,
                    'public.t100_guardian_account_enrollments','INSERT') AS enrollment_insert"""
        )
    except Exception:
        raise GuardianDurableHalted("GUARDIAN_ALERT_ROLE_CANNOT_BE_VERIFIED") from None
    if (row is None or row["read_only"]!="off" or
            any(row[x] is not True for x in (
                "incident_insert","incident_update","outbox_select","outbox_update")) or
            any(row[x] is not False for x in (
                "incident_delete","incident_truncate",
                "outbox_delete","outbox_truncate",
                "checkpoint_insert","checkpoint_update","checkpoint_delete",
                "trade_intent_insert","stop_update",
                "emergency_update","enrollment_insert"))):
        raise GuardianDurableHalted("GUARDIAN_ALERT_ROLE_HAS_UNSAFE_PRIVILEGES")


async def run(
    *, environ: Mapping[str,str]|None=None, cycles: int|None=None,
    connect: Callable[[str,str],Awaitable[Any]]=default_alert_connection,
    observe: Callable[[Mapping[str,str]],Awaitable[ReadOnlyCycleSummary]]=run_with_network,
    notifier_factory: Callable[...,Any]=T100LiveOpsNotifier,
    pause: Callable[[float],Awaitable[Any]]=asyncio.sleep,
    emit: Callable[[str],Any]=print,
) -> GuardianDurableCycle:
    env=os.environ if environ is None else environ
    config,dsn,webhook,ca_file=durable_config(env)
    if cycles is not None and (type(cycles) is not int or cycles<1):
        raise GuardianDurableHalted("GUARDIAN_DURABLE_CYCLE_LIMIT_INVALID")
    notifier=notifier_factory(webhook,cooldown_seconds=0)
    conn=None
    count=0
    consecutive=0
    try:
        conn=await connect(dsn,ca_file)
        await verify_alert_writer_role(conn)
        outbox=GuardianIncidentOutbox(conn,scope_sha256=env[
            "T100_GUARDIAN_ACCOUNT_KEY_SHA256"])
        await outbox.acquire_singleton()
        while True:
            count+=1
            try:
                result=await asyncio.wait_for(
                    observe(env),
                    timeout=min(120.0, float(config.interval_seconds)),
                )
                classified=_classify(result,count,consecutive)
                consecutive=(consecutive+1 if classified.alert else 0)
                hb=_classify(result,count,consecutive)
            except Exception:
                consecutive+=1
                hb=GuardianHeartbeat(
                    cycle=count,status="OBSERVER_SIGNED_GET_OR_DB_UNAVAILABLE",
                    result="UNCERTIFIED",alert=True,
                    consecutive_failures=consecutive,
                )
            # Commit incident BEFORE attempting Discord. Missing DB ACK is
            # a fatal, nonauthorizing error; never replace with untracked
            # "best effort" safety messaging.
            stage=await outbox.observe(status=hb.status,alert=hb.alert)
            delivered=0
            for event in await outbox.pending(limit=10):
                await outbox.mark_attempt(event.seq)
                try:
                    sent=await asyncio.wait_for(notifier.send(
                        OpsNotice(
                            key="t100-guardian-"+event.code,
                            title="T100 GUARDIAN INCIDENT",
                            description=(event.code+
                                " (durable read-only observer event; human review required)"),
                            severity=event.severity,
                        ),force=True,
                    ),timeout=8.0)
                except Exception:
                    sent=False
                if sent is not True:
                    break
                await outbox.mark_delivered(event.seq)
                delivered+=1
            outstanding=await outbox.pending_count()
            report=GuardianDurableCycle(
                status=hb.status,
                alert=hb.alert or stage.critical_latched or bool(outstanding),
                critical_latched=stage.critical_latched,
                pending=outstanding,
                delivered_this_cycle=delivered,
            )
            emit(json.dumps(asdict(report),sort_keys=True))
            if consecutive>=config.maximum_failures:
                return report
            if cycles is not None and count>=cycles:
                return report
            await pause(config.interval_seconds)
    except (GuardianIncidentHalted,GuardianDurableHalted):
        raise
    except Exception:
        raise GuardianDurableHalted("GUARDIAN_DURABLE_RUNTIME_FAILED") from None
    finally:
        if conn is not None:
            await conn.close()
        await notifier.close()


def main() -> int:
    parser=argparse.ArgumentParser(description="Dedicated T100 durable guardian")
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    if not args.run:
        print(json.dumps({
            "status":"GUARDIAN_DURABLE_NOT_STARTED",
            "entry_allowed":False,"live_authorization":False,
            "mexc_orders_sent":0,
        },sort_keys=True))
        return 0
    try:
        result=asyncio.run(run(cycles=1 if args.once else None))
        return 2 if result.alert else 0
    except Exception as exc:
        print(json.dumps({
            "status":"GUARDIAN_DURABLE_HALTED",
            "error_class":type(exc).__name__,
            "entry_allowed":False,"live_authorization":False,
            "mexc_orders_sent":0,
        },sort_keys=True))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
