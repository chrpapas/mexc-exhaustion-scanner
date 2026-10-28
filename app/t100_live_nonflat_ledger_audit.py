"""Manual-only, read-only audit of T100 durable non-flat ledger presence.

The existing MEXC position must never be adopted by matching a count.
This gate only discovers whether the separate T100 PostgreSQL ledger
contains an eligible *candidate* checkpoint/origin-intent/revision chain.

No MEXC API calls, no SQL writes, no exchange position IDs or symbols
printed, no live authorization, no mutations to paper scanner tables.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE

COUNT_FIELDS = (
    "active_checkpoints",
    "active_checkpoint_revisions",
    "ack_order_intents",
    "active_checkpoints_with_matching_ack_intent",
    "active_checkpoints_with_contiguous_revision_counts",
    "unresolved_order_intents",
    "unresolved_stop_amendments",
    "review_or_closing_checkpoints",
    "active_executor_leases",
)

# One repeatable-read snapshot: no account identities or economic data
# ever leave the database, even as intermediate connector output.
COUNTS_SQL = """
SELECT
  (SELECT count(*) FROM public.t100_live_position_checkpoints
    WHERE lifecycle_state <> 'CLOSED') AS active_checkpoints,
  (SELECT count(*) FROM public.t100_live_position_checkpoint_revisions r
    JOIN public.t100_live_position_checkpoints c
      ON c.position_id=r.position_id
    WHERE c.lifecycle_state <> 'CLOSED') AS active_checkpoint_revisions,
  (SELECT count(*) FROM public.t100_live_order_intents
    WHERE state='ACK') AS ack_order_intents,
  (SELECT count(*) FROM public.t100_live_position_checkpoints c
    JOIN public.t100_live_order_intents i
      ON i.external_oid=c.external_oid
    WHERE c.lifecycle_state <> 'CLOSED'
      AND i.state='ACK'
      AND i.exchange_order_id=c.exchange_order_id
      AND i.symbol=c.symbol) AS active_checkpoints_with_matching_ack_intent,
  (SELECT count(*) FROM public.t100_live_position_checkpoints c
    WHERE c.lifecycle_state <> 'CLOSED'
      AND (
          SELECT count(*)
          FROM public.t100_live_position_checkpoint_revisions r
          WHERE r.position_id=c.position_id
      ) = c.checkpoint_version
      AND EXISTS (
          SELECT 1 FROM public.t100_live_position_checkpoint_revisions r
          WHERE r.position_id=c.position_id AND r.checkpoint_version=1
      )
      AND EXISTS (
          SELECT 1 FROM public.t100_live_position_checkpoint_revisions r
          WHERE r.position_id=c.position_id
            AND r.checkpoint_version=c.checkpoint_version
      )
  ) AS active_checkpoints_with_contiguous_revision_counts,
  (SELECT count(*) FROM public.t100_live_order_intents
    WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW'))
    AS unresolved_order_intents,
  (SELECT count(*) FROM public.t100_live_stop_amendments
    WHERE state <> 'VERIFIED') AS unresolved_stop_amendments,
  (SELECT count(*) FROM public.t100_live_position_checkpoints
    WHERE lifecycle_state IN ('REVIEW','CLOSING'))
    AS review_or_closing_checkpoints,
  (SELECT count(*) FROM public.t100_live_executor_leases
    WHERE lease_name='t100-live-executor'
      AND lease_expires_at > now()) AS active_executor_leases
"""


def _blocked(reason: str, *, failure_type: str | None = None) -> dict[str, Any]:
    report = {
        "status": reason,
        "read_only": True,
        "database_name_verified": False,
        "restricted_runtime_role_verified": False,
        "repeatable_read_snapshot": False,
        "t100_nonflat_recovery_verified": False,
        "current_mexc_position_adopted": False,
        "exchange_read_performed": False,
        "live_order_submission_possible": False,
        "live_authorization": False,
        "database_writes_sent": False,
    }
    if failure_type:
        report["failure_type"] = failure_type
    return report


async def inspect_nonflat_ledger(conn: Any) -> dict[str, Any]:
    report = _blocked("LEDGER_AUDIT_NOT_STARTED")
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            readonly = await conn.fetchval(
                "SELECT current_setting('transaction_read_only')"
            )
            isolation = await conn.fetchval(
                "SELECT current_setting('transaction_isolation')"
            )
            if readonly != "on" or isolation != "repeatable read":
                report["status"] = "LEDGER_SNAPSHOT_NOT_READ_ONLY"
                return report
            report["repeatable_read_snapshot"] = True
            database = await conn.fetchval("SELECT current_database()")
            if database != EXPECTED_DATABASE:
                report["status"] = "LEDGER_WRONG_DATABASE"
                return report
            report["database_name_verified"] = True
            role = await conn.fetchval("SELECT current_user")
            if role != TARGET_ROLE:
                report["status"] = "LEDGER_WRONG_RUNTIME_ROLE"
                return report
            report["restricted_runtime_role_verified"] = True

            values = await conn.fetchrow(COUNTS_SQL)
            if values is None:
                report["status"] = "LEDGER_COUNTS_MISSING"
                return report
            counts: dict[str, int] = {}
            for name in COUNT_FIELDS:
                value = values[name]
                if type(value) is not int or value < 0:
                    report["status"] = "LEDGER_COUNT_INVALID"
                    return report
                counts[name] = value
            report.update(counts)

            active = counts["active_checkpoints"]
            if any((
                counts["unresolved_order_intents"],
                counts["unresolved_stop_amendments"],
                counts["review_or_closing_checkpoints"],
            )):
                report["status"] = "LEDGER_UNRESOLVED_STATE_BLOCKED"
            elif active == 0:
                report["status"] = "NO_ACTIVE_T100_DURABLE_CHECKPOINTS"
            elif (
                counts["active_checkpoints_with_matching_ack_intent"] != active
                or counts[
                    "active_checkpoints_with_contiguous_revision_counts"
                ] != active
            ):
                report["status"] = "LEDGER_RECOVERY_PROVENANCE_INCOMPLETE"
            else:
                report["status"] = (
                    "T100_CHECKPOINT_CANDIDATES_REQUIRE_EXCHANGE_RECONCILIATION"
                )
            return report
    except Exception as exc:
        # Never propagate DB exception text, which could carry query params.
        return _blocked(
            "LEDGER_READ_FAILED", failure_type=type(exc).__name__
        )


async def _main() -> None:
    from app.t100_live_guards import T100ExecutionControls
    flags = T100ExecutionControls.from_env()
    if (
        flags.mode != "paper"
        or flags.live_orders_enabled
        or not flags.kill_switch_engaged
    ):
        print(json.dumps(_blocked("LEDGER_AUDIT_REQUIRES_KILL_SWITCHED_PAPER_MODE")))
        raise SystemExit(2)

    password = getpass.getpass(
        "Existing restricted T100 runtime DB password (hidden): "
    )
    owner_url = getpass.getpass(
        "Existing Render PostgreSQL External URL for host/DB only (hidden): "
    )
    if not password or not owner_url:
        print(json.dumps(_blocked("LEDGER_DATABASE_CREDENTIALS_MISSING")))
        raise SystemExit(2)
    import asyncpg
    try:
        conn = await asyncpg.connect(
            dsn=owner_url,
            user=TARGET_ROLE,
            password=password,
            ssl="require",
            timeout=10,
            command_timeout=15,
        )
    except Exception as exc:
        print(json.dumps(_blocked(
            "LEDGER_CONNECTION_FAILED",
            failure_type=type(exc).__name__,
        ), indent=2, sort_keys=True))
        return
    try:
        report = await inspect_nonflat_ledger(conn)
        print(json.dumps(report, indent=2, sort_keys=True))
        print("SQL SELECT ONLY. NO MEXC REQUESTS. NO LIVE AUTHORIZATION.")
    finally:
        await conn.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only T100 durable checkpoint presence and provenance audit"
    )
    parser.parse_args()
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps(
            _blocked(
                "LEDGER_DIAGNOSTIC_HALTED",
                failure_type=type(exc).__name__,
            ),
            indent=2, sort_keys=True,
        ))
        raise SystemExit(2) from None


if __name__ == "__main__":
    cli()
