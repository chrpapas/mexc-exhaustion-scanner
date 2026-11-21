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
import sys
from typing import Any,Awaitable,Callable,Mapping
from urllib.parse import urlsplit

from app.t100_live_guardian_durable_worker import (
    default_alert_connection,verify_alert_writer_role,
)
from app.t100_live_guardian_incident_outbox import (
    GuardianIncidentHalted,GuardianIncidentOutbox,
)
from app.t100_live_ops_notifier import OpsNotice,T100LiveOpsNotifier
from app.t100_v40_canary_account_guardian import (
    V40CanaryGuardianHeartbeat,V40CanaryGuardianHalted,
    observe_v40_account_once,verify_guardian_environment,
)
from app.t100_v40_guardian_get_failure_codes import (
    SAFE_GUARDIAN_MODE_HALTS, SAFE_GUARDIAN_INVENTORY_DIAGNOSTIC_HALTS,
)

SCOPE=hashlib.sha256(b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN").hexdigest()


@dataclass(frozen=True)
class V40DurableGuardianStatus:
    status: str
    # Bounded fixed observer code only; never log an HTTP/SQL exception.
    observer_detail: str="NOT_OBSERVED"
    critical_latched: bool=True
    incident_pending: int=0
    discord_delivered: int=0
    account_flat_two_get: bool=False
    owned_cross_native_sl_observed: bool=False
    owned_cross_positions_count: int=0
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


# Hardcoded diagnostic vocabulary. Never print exception text from asyncpg,
# httpx, MEXC, Discord or a user-controlled connection string.
_SAFE_STARTUP_REASONS=frozenset({
    "V40_GUARDIAN_READONLY_ACCOUNT_SCOPE_REQUIRED",
    "V40_GUARDIAN_READONLY_CREDENTIALS_MISSING",
    "V40_GUARDIAN_MUST_HAVE_SEPARATE_GET_KEY",
    "V40_DURABLE_ALERTS_NOT_ENABLED",
    "V40_DURABLE_ACCOUNT_OR_DB_CONFIG_INVALID",
    "V40_DURABLE_WRITER_ROLE_NOT_SEPARATE",
    "V40_CROSS_OWNED_GUARDIAN_DISTINCT_READONLY_LEDGER_REQUIRED",
    "V40_DURABLE_DISCORD_WEBHOOK_INVALID",
    "V40_DURABLE_CYCLE_LIMIT_INVALID",
    "V40_DURABLE_DATABASE_CONNECT_FAILED_SAFE_HALT",
    "V40_DURABLE_INCIDENT_WRITER_ROLE_FAILED_SAFE_HALT",
    "V40_DURABLE_INCIDENT_SINGLETON_FAILED_SAFE_HALT",
    "V40_DURABLE_NOTIFIER_CONSTRUCT_FAILED_SAFE_HALT",
    "V40_DURABLE_INCIDENT_PERSIST_OR_DISCORD_FAILED_SAFE_HALT",
    "V40_DURABLE_GUARDIAN_UNEXPECTED_EXIT",
    "V40_SHARED_GUARDIAN_NOT_RESTRICTED_EXECUTOR_ROLE",
    "V40_SHARED_GUARDIAN_INCIDENT_GRANTS_NOT_APPLIED",
    "V40_SHARED_GUARDIAN_DATABASE_PERMISSION_AUDIT_FAILED",
    "V40_SHARED_GUARDIAN_DATABASE_CONNECT_FAILED",
    "V40_SHARED_GUARDIAN_TLS_MODE_INVALID",
    "V40_SHARED_GUARDIAN_CONNECTION_STRING_MISSING",
    "V40_SHARED_GUARDIAN_INCIDENT_GRANTS_NOT_APPLIED",
    "V40_SHARED_GUARDIAN_DATABASE_PERMISSION_AUDIT_FAILED",
    "V40_SHARED_GUARDIAN_MIGRATION_035_MISSING",
    "V40_AUDITED_ROLLOVER_REVIEWED_STATE_INVALID",
    "V40_AUDITED_ROLLOVER_LEGACY_NOT_RECOVERED",
    "V40_AUDITED_ROLLOVER_UNREVIEWED_ALERTS",
    "V40_AUDITED_ROLLOVER_FRESH_FLAT_UNVERIFIED",
    "V40_AUDITED_ROLLOVER_NEW_SCOPE_NOT_CLEAN",
    "V40_AUDITED_ROLLOVER_DATABASE_REVIEW_UNAVAILABLE",
    "V40_RECOVERY_003_PREDECESSOR_STATE_NOT_RECOVERED",
    "V40_RECOVERY_003_PREDECESSOR_ALERT_NOT_REVIEWED",
    "V40_RECOVERY_003_FRESH_FLAT_UNVERIFIED",
    "V40_RECOVERY_003_NEW_SCOPE_NOT_CLEAN",
    "V40_RECOVERY_003_DATABASE_REVIEW_UNAVAILABLE",
    "V40_RECOVERY_004_DB_CONNECTION_INVALID",
    "V40_RECOVERY_004_EXISTING_SCOPE_STATE_INVALID",
    "V40_RECOVERY_004_PREDECESSOR_STATE_NOT_REVIEWED",
    "V40_RECOVERY_004_PREDECESSOR_INCIDENT_MISMATCH",
    "V40_RECOVERY_004_FRESH_ACCOUNT_GET_OR_LEDGER_UNVERIFIED",
    "V40_RECOVERY_004_NEW_SCOPE_NOT_CLEAN",
    "V40_RECOVERY_004_DATABASE_OR_OBSERVER_UNAVAILABLE",
    "V40_RECOVERY_005_EXPLICIT_OPERATOR_REVIEW_REQUIRED",
    "V40_RECOVERY_005_EXISTING_SCOPE_STATE_INVALID",
    "V40_RECOVERY_005_PREDECESSOR_NOT_HEALTHY_OR_STICKY",
    "V40_RECOVERY_005_INCIDENT_HISTORY_OR_DELIVERY_MISMATCH",
    "V40_RECOVERY_005_FRESH_SIGNED_GET_UNAVAILABLE",
    "V40_RECOVERY_005_FRESH_FLAT_HEDGE_LEDGER_UNVERIFIED",
    "V40_RECOVERY_005_NEW_SCOPE_NOT_CLEAN",
    "V40_RECOVERY_005_DB_OR_ACCOUNT_REVIEW_UNAVAILABLE",
    "V40_RECOVERY_006_EXPLICIT_OPERATOR_APPROVAL_MISSING",
    "V40_RECOVERY_006_EXISTING_SCOPE_UNKNOWN",
    "V40_RECOVERY_006_OLD_STATE_OR_STICKY_LATCH_CHANGED",
    "V40_RECOVERY_006_CRITICAL_HISTORY_OR_DISCORD_NOT_EXACT",
    "V40_RECOVERY_006_SIGNED_GET_FAILED",
    "V40_RECOVERY_006_CURRENT_FLAT_LEDGER_NOT_VERIFIED",
    "V40_RECOVERY_006_NEW_SCOPE_NOT_CLEAN",
    "V40_RECOVERY_006_DB_OR_LIVE_ACCOUNT_REVIEW_UNAVAILABLE",
    "V40_RECOVERY_006_OUTBOX_SHAPE_INVALID",
    "V40_RECOVERY_006_005_CRITICAL_CLASS_OR_COUNT_MISMATCH",
    "V40_RECOVERY_006_OLDER_CRITICAL_CLASS_OR_COUNT_MISMATCH",
    "V40_RECOVERY_006_DISCORD_PENDING_INCIDENT",
    "V40_RECOVERY_006_005_HTTP_5XX_TIME_WINDOW_MISMATCH",
    "V40_RECOVERY_006_OTHER_EPOCH_NOV19_CRITICAL_PRESENT",
    "V40_RECOVERY_006_OLD_INCIDENT_TIME_WINDOWS_MISMATCH",
    "V40_RECOVERY_007_005_CRITICAL_CLASS_OR_COUNT_MISMATCH",
    "V40_RECOVERY_007_005_HTTP_5XX_TIME_WINDOW_MISMATCH",
    "V40_RECOVERY_007_006_TIMEOUT_INCIDENT_MISMATCH",
    "V40_RECOVERY_007_CURRENT_FLAT_LEDGER_NOT_VERIFIED",
    "V40_RECOVERY_007_DB_OR_LIVE_ACCOUNT_REVIEW_UNAVAILABLE",
    "V40_RECOVERY_007_DISCORD_PENDING_INCIDENT",
    "V40_RECOVERY_007_EXISTING_SCOPE_UNKNOWN",
    "V40_RECOVERY_007_EXPLICIT_OPERATOR_APPROVAL_MISSING",
    "V40_RECOVERY_007_NEW_SCOPE_NOT_CLEAN",
    "V40_RECOVERY_007_OLDER_CRITICAL_CLASS_OR_COUNT_MISMATCH",
    "V40_RECOVERY_007_OLD_INCIDENT_TIME_WINDOWS_MISMATCH",
    "V40_RECOVERY_007_OLD_STATE_OR_STICKY_LATCH_CHANGED",
    "V40_RECOVERY_007_OTHER_EPOCH_NOV19_CRITICAL_PRESENT",
    "V40_RECOVERY_007_OUTBOX_SHAPE_INVALID",
    "V40_RECOVERY_007_SIGNED_GET_FAILED",
})


_SAFE_OBSERVER_DETAILS=frozenset({
    "V40_GUARDIAN_INDEPENDENT_CREDENTIAL_SCOPE_HALT",
    "V40_GUARDIAN_DISTINCT_LEDGER_READER_NOT_PROVISIONED_HALT",
    "V40_GUARDIAN_INDEPENDENT_DB_OR_VENUE_GET_FAILED_HALT",
    "V40_GUARDIAN_INDEPENDENT_READER_OR_GET_INVALID_HALT",
    "V40_GUARDIAN_ACCOUNT_HEDGE_MODE_UNVERIFIED_HALT",
    "V40_GUARDIAN_ACCOUNT_LEDGER_OR_GET_UNAVAILABLE_HALT",
    "V40_GUARDIAN_READONLY_COMPONENTS_INIT_FAILED_HALT",
    "V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT",
    "V40_GUARDIAN_SNAPSHOT_RUNTIME_EXCEPTION_HALT",
    "V40_GUARDIAN_UNOWNED_OR_UNKNOWN_PENDING_MUTATION_HALT",
    "V40_GUARDIAN_OWNED_CROSS_AUDIT_FAILED_HALT",
    "V40_GUARDIAN_OWNED_CROSS_OR_NATIVE_SL_UNVERIFIED_HALT",
    "V40_GUARDIAN_MULTI_OWNED_AUDIT_RUNTIME_HALT",
    "V40_GUARDIAN_MULTI_OWNED_CROSS_OR_NATIVE_SL_UNVERIFIED_HALT",
    "V40_GUARDIAN_TWO_TO_FOUR_OWNED_CROSS_NATIVE_SL_OBSERVED_NONAUTHORIZING",
    "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING",
    "V40_GUARDIAN_ONE_OWNED_CROSS_SHORT_NATIVE_SL_OBSERVED_NONAUTHORIZING",
    "V40_GUARDIAN_SIGNED_GET_OR_RUNTIME_FAILED_HALT",
    "V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT",
    "V40_GUARDIAN_READONLY_SCOPE_OR_KEY_INVALID_HALT",
    "V40_GUARDIAN_ACCOUNT_NETWORK_OR_TIMEOUT_HALT",
    "V40_GUARDIAN_SIGNED_GET_UNAVAILABLE_HALT",
    "V40_GUARDIAN_ACCOUNT_SHAPE_OR_HEDGE_UNVERIFIED_HALT",
    "V40_GUARDIAN_ACCOUNT_BOOKENDS_DRIFT_HALT",
}) | SAFE_GUARDIAN_MODE_HALTS | SAFE_GUARDIAN_INVENTORY_DIAGNOSTIC_HALTS


def _safe_observer_detail(heartbeat:object)->str:
    code=heartbeat.status if type(heartbeat) is V40CanaryGuardianHeartbeat else None
    if type(code) is str and code in _SAFE_OBSERVER_DETAILS:
        return code
    if (
        type(code) is str
        and code.startswith("V40_GUARDIAN_SNAPSHOT_")
        and code.endswith("_HALT")
    ):
        # Only approved fixed coordinator codes, not exchange/SQL contents.
        from app.t100_v40_guardian_account_and_owned import (
            _SAFE_SNAPSHOT_NONREADY,
        )
        snapshot_code=code[
            len("V40_GUARDIAN_SNAPSHOT_"):-len("_HALT")
        ]
        if snapshot_code in _SAFE_SNAPSHOT_NONREADY:
            return code
    if (
        type(code) is str
        and code.startswith("V40_GUARDIAN_INVENTORY_")
        and code.endswith("_HALT")
    ):
        from app.t100_v40_guardian_account_and_owned import (
            _SAFE_INVENTORY_CERT_FAILURES,
        )
        certificate_code=code[
            len("V40_GUARDIAN_INVENTORY_"):-len("_HALT")
        ]
        if certificate_code in _SAFE_INVENTORY_CERT_FAILURES:
            return code
    if (
        type(code) is str
        and code.startswith("V40_GUARDIAN_INVENTORY_STAGE_")
        and code.endswith("_HALT")
    ):
        from app.t100_v40_guardian_account_and_owned import (
            _SAFE_INVENTORY_PROBE_STAGES,
        )
        stage=code[len("V40_GUARDIAN_INVENTORY_STAGE_"):-len("_HALT")]
        if stage in _SAFE_INVENTORY_PROBE_STAGES:
            return code
    return "V40_GUARDIAN_UNCLASSIFIED_OBSERVER_HALT"


def _redacted_startup_status(error:BaseException)->str:
    # Exact equality with a bounded constant list prevents leaking raw
    # database errors, URLs, passwords, API tokens or Discord webhook paths.
    status=str(error)
    return status if status in _SAFE_STARTUP_REASONS else (
        "V40_DURABLE_GUARDIAN_FAILED_SAFE_HALT"
    )


def config(env:Mapping[str,str])->tuple[str,str,str,int]:
    verify_guardian_environment(env)
    if env.get("T100_V40_GUARDIAN_DURABLE_ALERTS_ENABLED")!="true":
        raise V40DurableGuardianHalted("V40_DURABLE_ALERTS_NOT_ENABLED")
    shared=env.get("T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB")=="true"
    dsn=env.get(
        "T100_V40_GUARDIAN_SHARED_DB_DSN" if shared
        else "T100_V40_GUARDIAN_INCIDENT_DB_DSN",""
    )
    ca=(
        "USE_EXISTING_V40_RUNTIME_TLS" if shared
        else env.get("T100_V40_GUARDIAN_INCIDENT_DB_CA_FILE","")
    )
    hook=env.get("T100_V40_GUARDIAN_DISCORD_WEBHOOK_URL","")
    interval=env.get("T100_V40_CANARY_GUARDIAN_INTERVAL_SECONDS","5")
    if (
        type(dsn) is not str or not dsn.startswith(
            ("postgresql://","postgres://")
        )
        or (
            not shared and
            (type(ca) is not str or not ca.startswith("/"))
        )
        or type(hook) is not str
        or interval not in ("3","4","5")
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_ACCOUNT_OR_DB_CONFIG_INVALID")
    if shared:
        # Same exact DSN as the existing V40 trader runtime, NOT the
        # database owner or scanner signal-reader login. Validate actual
        # current_user and grant shape on connection; do not trust this flag.
        if dsn==env.get("T100_V40_SIGNAL_READER_DSN") and dsn:
            raise V40DurableGuardianHalted(
                "V40_DURABLE_WRITER_ROLE_NOT_SEPARATE"
            )
    elif dsn==env.get("T100_V40_SIGNAL_READER_DSN") or dsn==env.get(
        "T100_V40_EXECUTOR_RUNTIME_DSN"
    ):
        raise V40DurableGuardianHalted("V40_DURABLE_WRITER_ROLE_NOT_SEPARATE")
    if (
        not shared and
        env.get("T100_V40_GUARDIAN_CROSS_PROTECTION_REQUIRED")=="true"
    ):
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
            "V40_GUARDIAN_TWO_TO_FOUR_OWNED_CROSS_NATIVE_SL_OBSERVED_NONAUTHORIZING"
        and type(heartbeat.owned_cross_positions_count) is int
        and 2 <= heartbeat.owned_cross_positions_count <= 4
        and heartbeat.all_owned_cross_native_market_sl_verified is True
        and heartbeat.single_owned_cross_native_sl_verified is False
        and heartbeat.ledger_provenance_verified is True
        and heartbeat.inventory_two_signed_get_bookends is True
        and heartbeat.hedge_mode_verified is True
        and heartbeat.incident_alert is False
        and heartbeat.mexc_posts==heartbeat.database_writes==0
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
    shared=env.get("T100_V40_GUARDIAN_REUSE_V40_RUNTIME_DB")=="true"
    reviewed=env.get("T100_V40_GUARDIAN_AUDITED_ROLLOVER")=="true"
    if reviewed:
        from app.t100_v40_guardian_reviewed_generation import (
            V40GuardianReviewedGenerationHalted,
        )
        from app.t100_v40_guardian_recovery_004 import (
            select_fourth_reviewed_scope,V40GuardianRecovery004Halted,
        )
        from app.t100_v40_guardian_recovery_005 import (
            select_fifth_reviewed_scope,V40GuardianRecovery005Halted,
        )
        from app.t100_v40_guardian_recovery_006 import (
            select_sixth_reviewed_scope,V40GuardianRecovery006Halted,
        )
        from app.t100_v40_guardian_recovery_007 import (
            select_seventh_reviewed_scope,V40GuardianRecovery007Halted,
        )
    if shared:
        from app.t100_v40_guardian_shared_db import (
            V40SharedGuardianDatabaseHalted,
            shared_guardian_connection,
            verify_existing_runtime_for_guardian,
        )
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
    stage="DATABASE_CONNECT"
    try:
        connector=(
            shared_guardian_connection
            if shared and connect is default_alert_connection
            else connect
        )
        conn=await connector(dsn,ca)
        stage="INCIDENT_WRITER_ROLE"
        if shared:
            await verify_existing_runtime_for_guardian(conn)
        else:
            await verify_alert_writer_role(conn)
        stage="INCIDENT_SINGLETON"
        outbox=GuardianIncidentOutbox(conn,scope_sha256=SCOPE)
        # During Render worker rollout the retiring observer can still
        # hold the same PG advisory lock briefly. Do NOT permit concurrent
        # account observation: only wait for a real singleton handoff.
        # A different singleton error or prolonged contention fails closed.
        for handoff in range(60):
            try:
                await outbox.acquire_singleton()
                break
            except GuardianIncidentHalted as error:
                if (
                    str(error)!="GUARDIAN_INCIDENT_ALREADY_RUNNING"
                    or handoff==59
                ):
                    raise
                await pause(3.0)
        if reviewed:
            # Old scope 004 remains sticky. A NEW 005 is created only
            # on an explicit one-time operator opt-in, exact full incident
            # database audit and two independent fresh signed GET/ledger
            # bookends. All five old state rows and alerts remain immutable.
            # No opt-in -> unchanged 004 behavior and no release.
            stage="AUDITED_ROLLOVER_REVIEW"
            if env.get(
                "T100_V40_GUARDIAN_RECOVERY_007_OPERATOR_APPROVED"
            )=="true":
                reviewed_scope,reviewed_reason=await select_seventh_reviewed_scope(
                    conn,env=env,observe=observer,pause=pause,
                )
            elif env.get(
                "T100_V40_GUARDIAN_RECOVERY_006_OPERATOR_APPROVED"
            )=="true":
                reviewed_scope,reviewed_reason=await select_sixth_reviewed_scope(
                    conn,env=env,observe=observer,pause=pause,
                )
            elif env.get(
                "T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED"
            )=="true":
                reviewed_scope,reviewed_reason=await select_fifth_reviewed_scope(
                    conn,env=env,observe=observer,pause=pause,
                )
            else:
                reviewed_scope,reviewed_reason=await select_fourth_reviewed_scope(
                    conn,env=env,observe=observer,pause=pause,
                )
            outbox=GuardianIncidentOutbox(
                conn,scope_sha256=reviewed_scope,
            )
            emit(json.dumps({
                "status":"V40_AUDITED_GUARDIAN_REVIEWED_SCOPE_ACTIVE",
                "reviewed_reason":reviewed_reason,
                "old_latch_untouched":True,
                "entry_allowed":False,
                "live_authorization":False,
                "mexc_posts":0,
            },sort_keys=True))
            if emit is print:
                sys.stdout.flush()
        # Opt-in one-shot SELECT-only forensic report. This does not change
        # the sticky scope, alert delivery, monitoring, or POST gate.
        if (
            reviewed and
            env.get("T100_V40_GUARDIAN_READONLY_ARCHIVE_AUDIT_ENABLED")=="true"
        ):
            try:
                from app.t100_v40_guardian_incident_archive_audit import (
                    audit_guardian_archive_readonly,
                )
                audit=await audit_guardian_archive_readonly(conn)
                emit(json.dumps(audit.safe_json(),sort_keys=True))
            except Exception:
                emit(json.dumps({
                    "status":"V40_GUARDIAN_ARCHIVE_READONLY_AUDIT_UNAVAILABLE",
                    "entry_allowed":False,"live_authorization":False,
                    "mexc_posts":0,"database_writes":0,
                },sort_keys=True))
            if emit is print:
                sys.stdout.flush()
        stage="NOTIFIER_CONSTRUCT"
        notifier=notifier_factory(hook,cooldown_seconds=0)
        stage="INCIDENT_PERSIST_OR_DISCORD"
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
                observer_detail=_safe_observer_detail(heartbeat),
                critical_latched=state.critical_latched,
                incident_pending=pending_count,
                discord_delivered=delivered,
                account_flat_two_get=(
                    code=="OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
                ),
                owned_cross_native_sl_observed=(
                    code=="OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
                ),
                owned_cross_positions_count=(
                    heartbeat.owned_cross_positions_count if (
                        type(heartbeat) is V40CanaryGuardianHeartbeat
                        and type(heartbeat.owned_cross_positions_count) is int
                        and 0<=heartbeat.owned_cross_positions_count<=4
                        and code=="OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
                    ) else 0
                ),
                guardian_outbox_committed=True,
            )
            emit(json.dumps(asdict(report),sort_keys=True))
            if emit is print:
                # Long-running Render Python workers can buffer stdout,
                # hiding the guardian's 5-second GET-only heartbeats.
                sys.stdout.flush()
            # If a historical critical incident remains latched but the
            # current signed GET is healthy, keep observing every cycle.
            # Never silently clear the durable latch or give trade approval.
            # A fresh unsafe observation still halts this worker immediately.
            if not healthy:
                return report
            if max_cycles is not None and cycle>=max_cycles:
                return report
            await pause(float(interval))
        raise V40DurableGuardianHalted("V40_DURABLE_GUARDIAN_UNEXPECTED_EXIT")
    except V40DurableGuardianHalted:
        raise
    except Exception as error:
        # Distinct fixed status codes may be exposed; never raw connection
        # errors, API responses, SQL parameters or Discord URLs.
        if reviewed and isinstance(error,(
            V40GuardianReviewedGenerationHalted,V40GuardianRecovery004Halted,
            V40GuardianRecovery005Halted,V40GuardianRecovery006Halted,
            V40GuardianRecovery007Halted,
        )):
            reason=str(error)
            if reason in _SAFE_STARTUP_REASONS:
                raise V40DurableGuardianHalted(reason) from None
        if shared and isinstance(error,V40SharedGuardianDatabaseHalted):
            reason=str(error)
            if reason in _SAFE_STARTUP_REASONS:
                raise V40DurableGuardianHalted(reason) from None
        raise V40DurableGuardianHalted(
            "V40_DURABLE_"+stage+"_FAILED_SAFE_HALT"
        ) from None
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
    except (V40DurableGuardianHalted,V40CanaryGuardianHalted) as error:
        print(json.dumps(asdict(V40DurableGuardianStatus(
            _redacted_startup_status(error)
        )),sort_keys=True),flush=True)
        return 2
    except Exception:
        print(json.dumps(asdict(V40DurableGuardianStatus(
            "V40_DURABLE_GUARDIAN_FAILED_SAFE_HALT"
        )),sort_keys=True),flush=True)
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
